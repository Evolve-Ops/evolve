/**
 * SessionClassifier
 *
 * Classifies a session as 'productive' (objective work) or 'maintenance'
 * (bot system upkeep). These are session-level labels, distinct from
 * model tiers (tier0-tier3) which describe compute/cost class.
 *
 * productive — moves human objectives forward:
 *   Research, writing, planning, answering questions, completing tasks
 *   for the user's life, work, or projects.
 *
 * maintenance — maintains the bot system itself:
 *   Fixing config errors, debugging bots, restarting gateways, resolving
 *   permissions, watchdog issues, etc.
 *
 * NOTE: maintenance is not inherently bad — it's evidence that something
 * in the system required it. The maintenance_ratio metric tracks this and
 * the analyzer proposes fixes to reduce the root causes.
 *
 * v0.1: Keyword-based only. LLM-based classifier added in v0.3.
 * v0.4: Reads {sharedDir}/calibration/classifier.json at startup to apply
 *       RSI-learned keyword additions/removals and confidence param overrides.
 */
export type SessionClass = "productive" | "maintenance" | "ambiguous";
export interface SessionClassResult {
    class: SessionClass;
    signals: string[];
    confidence: number;
}
export declare const CORRECTION_PATTERNS: string[];
/**
 * Load classifier calibration overrides from {sharedDir}/calibration/classifier.json.
 * Must be called once at plugin startup before any classification occurs.
 * Safe to call if the file is absent — defaults are preserved.
 */
export declare function loadCalibrationOverrides(sharedDir: string): void;
/**
 * Return the calibrated CORRECTION_PATTERNS array (base + RSI-learned deltas).
 * Used by TurnObserver so correction detection also benefits from calibration.
 */
export declare function getCalibratedCorrectionPatterns(): string[];
export declare function classifyTierByKeywords(userMessage: string, assistantMessage: string, _sessionId: string, hints?: {
    productive_extra?: string[];
    maintenance_extra?: string[];
}): SessionClassResult;
//# sourceMappingURL=TierClassifier.d.ts.map