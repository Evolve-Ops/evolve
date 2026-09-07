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

import * as fs from "fs";
import * as path from "path";

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
// provider-literal-allow-begin: offline pricing DATA (mirrors turn_cost.py)
export const EXACT_MODEL_COSTS: Record<string, ModelRates> = {
  // Anthropic
  "anthropic/claude-haiku-4-6":         { input: 0.80,  output: 4.00,  cacheWrite: 1.00,  cacheRead: 0.08 },
  "anthropic/claude-haiku-4-5":         { input: 0.80,  output: 4.00,  cacheWrite: 1.00,  cacheRead: 0.08 },
  "anthropic/claude-haiku-3-5":         { input: 0.80,  output: 4.00,  cacheWrite: 1.00,  cacheRead: 0.08 },
  "anthropic/claude-3-haiku":           { input: 0.25,  output: 1.25,  cacheWrite: 0.30,  cacheRead: 0.03 },
  "anthropic/claude-sonnet-4-6":        { input: 3.00,  output: 15.00, cacheWrite: 3.75,  cacheRead: 0.30 },
  "anthropic/claude-sonnet-4-5":        { input: 3.00,  output: 15.00, cacheWrite: 3.75,  cacheRead: 0.30 },
  "anthropic/claude-sonnet-4-20250514": { input: 3.00,  output: 15.00, cacheWrite: 3.75,  cacheRead: 0.30 },
  "anthropic/claude-sonnet-4-20250219": { input: 3.00,  output: 15.00, cacheWrite: 3.75,  cacheRead: 0.30 },
  "anthropic/claude-3-5-sonnet":        { input: 3.00,  output: 15.00, cacheWrite: 3.75,  cacheRead: 0.30 },
  "anthropic/claude-3-sonnet":          { input: 3.00,  output: 15.00, cacheWrite: 3.75,  cacheRead: 0.30 },
  "anthropic/claude-opus-4-6":          { input: 15.00, output: 75.00, cacheWrite: 18.75, cacheRead: 1.50 },
  "anthropic/claude-opus-4-5":          { input: 15.00, output: 75.00, cacheWrite: 18.75, cacheRead: 1.50 },
  "anthropic/claude-3-opus":            { input: 15.00, output: 75.00, cacheWrite: 18.75, cacheRead: 1.50 },
  // OpenAI — no prompt-cache write price; cacheRead is the discounted rate.
  "openai/gpt-4o":                      { input: 2.50,  output: 10.00, cacheWrite: 0.00,  cacheRead: 1.25 },
  "openai/gpt-4o-mini":                 { input: 0.15,  output: 0.60,  cacheWrite: 0.00,  cacheRead: 0.075 },
  "openai/gpt-4.1":                     { input: 2.00,  output: 8.00,  cacheWrite: 0.00,  cacheRead: 1.00 },
  "openai/gpt-4.1-mini":                { input: 0.40,  output: 1.60,  cacheWrite: 0.00,  cacheRead: 0.20 },
  "openai/gpt-4.1-nano":                { input: 0.10,  output: 0.40,  cacheWrite: 0.00,  cacheRead: 0.05 },
  "openai/o3":                          { input: 10.00, output: 40.00, cacheWrite: 0.00,  cacheRead: 2.50 },
  "openai/o4-mini":                     { input: 1.10,  output: 4.40,  cacheWrite: 0.00,  cacheRead: 0.275 },
  // Google
  "google/gemini-3.1-pro-preview":      { input: 1.25,  output: 10.00, cacheWrite: 0.00,  cacheRead: 0.00 },
  "google/gemini-2.5-pro-preview":      { input: 1.25,  output: 10.00, cacheWrite: 0.00,  cacheRead: 0.00 },
  "google/gemini-1.5-pro":              { input: 1.25,  output: 5.00,  cacheWrite: 0.00,  cacheRead: 0.00 },
  "google/gemini-2.0-flash":            { input: 0.10,  output: 0.40,  cacheWrite: 0.00,  cacheRead: 0.00 },
  "google/gemini-2.0-flash-lite":       { input: 0.075, output: 0.30,  cacheWrite: 0.00,  cacheRead: 0.00 },
  // xAI
  "xai/grok-3":                         { input: 3.00,  output: 15.00, cacheWrite: 0.00,  cacheRead: 0.00 },
  "xai/grok-3-mini":                    { input: 0.30,  output: 0.50,  cacheWrite: 0.00,  cacheRead: 0.00 },
  "xai/grok-4-1-fast":                  { input: 5.00,  output: 25.00, cacheWrite: 0.00,  cacheRead: 0.00 },
};
// provider-literal-allow-end

const PRICING_CACHE_NAME = "model-pricing.json";
const MTOK = 1_000_000;

/** Bare model id → its single qualified key, when the id is unambiguous
 *  across the table. An id present under two providers is omitted: a bare
 *  lookup that could mean either is not an exact match. */
const TABLE_BY_BARE_ID: Map<string, ModelRates> = (() => {
  const seen = new Map<string, ModelRates | null>();
  for (const [key, rates] of Object.entries(EXACT_MODEL_COSTS)) {
    const bare = key.slice(key.indexOf("/") + 1);
    seen.set(bare, seen.has(bare) ? null : rates);
  }
  const out = new Map<string, ModelRates>();
  for (const [bare, rates] of seen) if (rates) out.set(bare, rates);
  return out;
})();

interface CatalogIndex {
  /** "provider/model_id" → the normalized record. */
  byKey: Map<string, Record<string, unknown>>;
  /** bare model_id → record, only where the id names exactly one row. */
  byBareId: Map<string, Record<string, unknown>>;
  /** ISO-8601 the catalog was refreshed, when the document carries one. */
  refreshedAt: string | null;
}

/** Memo keyed on the catalog path, invalidated on mtime change. A gateway
 *  runs for weeks; a catalog refreshed underneath it must be picked up on
 *  the next call, not at the next restart. */
const catalogCache = new Map<string, { mtimeMs: number; index: CatalogIndex | null }>();

/** Test seam: drop the in-process catalog memo. */
export function _resetPricingCatalogCache(): void {
  catalogCache.clear();
}

function buildIndex(doc: unknown): CatalogIndex | null {
  const models = (doc as { models?: unknown })?.models;
  if (!Array.isArray(models)) return null;
  const byKey = new Map<string, Record<string, unknown>>();
  const bareSeen = new Map<string, Record<string, unknown> | null>();
  for (const raw of models) {
    if (!raw || typeof raw !== "object") continue;
    const rec = raw as Record<string, unknown>;
    const provider = String(rec.provider ?? "").trim().toLowerCase();
    const modelId = String(rec.model_id ?? "").trim();
    if (!provider || !modelId) continue;
    byKey.set(`${provider}/${modelId.toLowerCase()}`, rec);
    const bare = modelId.toLowerCase();
    bareSeen.set(bare, bareSeen.has(bare) ? null : rec);
  }
  const byBareId = new Map<string, Record<string, unknown>>();
  for (const [bare, rec] of bareSeen) if (rec) byBareId.set(bare, rec);
  const refreshed = (doc as { refreshed_at?: unknown })?.refreshed_at;
  return {
    byKey,
    byBareId,
    refreshedAt: typeof refreshed === "string" ? refreshed : null,
  };
}

/**
 * The pod's mirrored pricing catalog, or ``null`` when this pod has none.
 *
 * ``null`` means "no catalog here" — never "this model is free". Every
 * failure mode (missing file, unreadable, malformed JSON) collapses to it,
 * because the caller's next step is the same in all three: fall to the
 * offline table, then to unpriced.
 */
export function loadPricingCatalog(sharedDir: string | null | undefined): CatalogIndex | null {
  const dir = (sharedDir || "").trim();
  if (!dir) return null;
  const file = path.join(dir, PRICING_CACHE_NAME);
  let mtimeMs: number;
  try {
    mtimeMs = fs.statSync(file).mtimeMs;
  } catch {
    catalogCache.delete(file);
    return null;
  }
  const hit = catalogCache.get(file);
  if (hit && hit.mtimeMs === mtimeMs) return hit.index;
  let index: CatalogIndex | null = null;
  try {
    index = buildIndex(JSON.parse(fs.readFileSync(file, "utf8")));
  } catch {
    index = null;
  }
  catalogCache.set(file, { mtimeMs, index });
  return index;
}

/** ``refreshed_at`` from the mirrored catalog, or null. Read by the freshness
 *  line on the receipt's Python side via its own reader; exported here so a
 *  plugin-side surface never has to re-parse the file. */
export function catalogRefreshedAt(sharedDir: string | null | undefined): string | null {
  return loadPricingCatalog(sharedDir)?.refreshedAt ?? null;
}

/** Split a model id into the segments a lookup may try, mirroring
 *  ``turn_cost._catalog_candidates``: a gateway records the model under its
 *  VENDOR (``anthropic/claude-…``) while ``provider`` names the gateway. */
function candidateKeys(model: string, provider: string): { keys: string[]; bare: string } {
  const segs = model.split("/").map((s) => s.trim()).filter(Boolean);
  if (segs.length === 0) return { keys: [], bare: "" };
  if (segs.length > 1 && segs[0] === provider) segs.shift();
  const bare = segs[segs.length - 1];
  const joined = segs.join("/");
  const vendor = segs.length > 1 ? segs[0] : "";
  const keys: string[] = [];
  for (const prov of [provider, vendor]) {
    if (!prov || prov === "unknown") continue;
    for (const id of [bare, joined]) {
      const key = `${prov}/${id}`;
      if (!keys.includes(key)) keys.push(key);
    }
  }
  return { keys, bare };
}

function rateFromRecord(rec: Record<string, unknown>, field: string): number | null {
  const raw = rec[field];
  if (raw === null || raw === undefined) return null;
  const n = typeof raw === "number" ? raw : Number(raw);
  return Number.isFinite(n) ? n * MTOK : null;
}

/** Catalog rates for (model, provider), or null when it has no row.
 *  Cache rates resolve catalog → offline table for the same model → the
 *  input rate. Each step is a published rate for that model; the last
 *  OVER-states a cache-heavy turn, which for a spend cap is the safe
 *  direction. Same order as ``turn_cost._catalog_pricing``. */
function catalogRates(
  index: CatalogIndex, model: string, provider: string, tableRates: ModelRates | null,
): ModelRates | null {
  const { keys, bare } = candidateKeys(model, provider);
  let rec: Record<string, unknown> | undefined;
  for (const key of keys) {
    rec = index.byKey.get(key.toLowerCase());
    if (rec) break;
  }
  if (!rec && bare) rec = index.byBareId.get(bare.toLowerCase());
  if (!rec) return null;
  const input = rateFromRecord(rec, "input_cost_per_token");
  const output = rateFromRecord(rec, "output_cost_per_token");
  if (input === null || output === null) return null;
  const cacheWrite = rateFromRecord(rec, "cache_write_cost_per_token");
  const cacheRead = rateFromRecord(rec, "cache_read_cost_per_token");
  return {
    input,
    output,
    cacheWrite: cacheWrite ?? tableRates?.cacheWrite ?? input,
    cacheRead: cacheRead ?? tableRates?.cacheRead ?? input,
  };
}

/** Offline-table rates for (model, provider), or null. Exact ids only. */
function tableRatesFor(model: string, provider: string): ModelRates | null {
  const { keys, bare } = candidateKeys(model, provider);
  for (const key of keys) {
    const hit = EXACT_MODEL_COSTS[key];
    if (hit) return hit;
  }
  return (bare && TABLE_BY_BARE_ID.get(bare)) || null;
}

function applyRates(
  r: ModelRates,
  inputTokens: number, outputTokens: number,
  cacheWriteTokens: number, cacheReadTokens: number,
): number {
  const cost =
    (inputTokens / MTOK) * r.input +
    (outputTokens / MTOK) * r.output +
    (cacheWriteTokens / MTOK) * r.cacheWrite +
    (cacheReadTokens / MTOK) * r.cacheRead;
  return Math.round(cost * MTOK) / MTOK; // 6 decimal places
}

/**
 * Price one model call: catalog by exact id → offline table by exact id →
 * unpriced. ``sharedDir`` is where the mirrored catalog lives; omit it and
 * only the offline table is available.
 */
export function priceCall(
  model: string,
  inputTokens: number,
  outputTokens: number,
  cacheWriteTokens: number,
  cacheReadTokens: number,
  provider?: string | null,
  sharedDir?: string | null,
): PricedCall {
  const lowerModel = (model || "").toLowerCase().trim();
  const prov = (provider || (lowerModel.includes("/") ? lowerModel.split("/")[0] : ""))
    .toLowerCase().trim();
  const table = tableRatesFor(lowerModel, prov);
  const index = loadPricingCatalog(sharedDir);
  const rates = index ? catalogRates(index, lowerModel, prov, table) : null;
  const resolved = rates ?? table;
  if (!resolved) return { costUsd: null, costSource: "unpriced" };
  return {
    costUsd: applyRates(
      resolved, inputTokens, outputTokens, cacheWriteTokens, cacheReadTokens,
    ),
    costSource: rates ? "catalog" : "table",
  };
}

/**
 * Numeric shim for the in-process breakers (SessionCostMonitor, the
 * runaway-rate cap) that must add a number to a running total.
 *
 * A ``0`` from here means "could not price", not "free" — the same contract
 * the pre-2026-09 estimator had, and the reason those callers treat a zero
 * as contributing nothing. The honest ``null`` reaches the readers that can
 * act on it, via ``priceCall`` and the turn record's ``cost_source``.
 */
export function estimateCost(
  model: string,
  inputTokens: number,
  outputTokens: number,
  cacheWriteTokens: number,
  cacheReadTokens: number,
  provider?: string | null,
  sharedDir?: string | null,
): number {
  return priceCall(
    model, inputTokens, outputTokens, cacheWriteTokens, cacheReadTokens,
    provider, sharedDir,
  ).costUsd ?? 0;
}
