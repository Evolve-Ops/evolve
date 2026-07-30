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

import * as fs from "fs";
import * as path from "path";

export type SessionClass = "productive" | "maintenance" | "ambiguous";

export interface SessionClassResult {
  class: SessionClass;
  signals: string[];
  confidence: number;
}

// Strong maintenance signals — infrastructure/system work
const TIER2_KEYWORDS: string[] = [
  // Config/system
  "openclaw.json", "gateway", "restart gateway", "gateway restart",
  "config error", "permission denied", "permission error", "launchd",
  "plist", "launchctl", "cron job", "cron edit", "cron error",
  "auth-profiles", "lastgood", "api key", "api fallback",
  // Debugging patterns
  "error:", "exit code", "command not found", "sudo", "chmod", "chown",
  "could not access", "failed to", "traceback", "exception",
  // Bot/watchdog infrastructure
  "watchdog", "consolidate-warnings", "usage-watchdog", "softthreshold",
  "isolatedsession", "context bloat", "cache write", "token limit",
  // Update/fix language
  "fix the", "fixing", "debug", "troubleshoot", "why is it", "what went wrong",
];

// Strong productive signals — objective/user work.
// These are generic patterns that apply across deployments.
// Deployment-specific keywords (project names, team members, etc.)
// should be added via network.json classifierHints.productive_extra.
const TIER1_KEYWORDS: string[] = [
  // Research and writing
  "research", "draft", "write", "create", "generate", "outline", "summary",
  // Planning and decisions
  "plan", "roadmap", "proposal", "architecture", "how should we",
  "what do you think", "recommend", "suggestion", "design",
  // Information requests
  "what is", "who is", "when did", "explain", "tell me about",
  "history of", "notes for",
  // Personal/productivity assistance
  "calendar", "weather", "appointment", "reminder", "travel",
  "health", "schedule", "agenda", "task", "meeting",
  // Project work (generic)
  "project", "report", "presentation", "document", "budget", "timeline",
];

// Correction detection patterns
export const CORRECTION_PATTERNS: string[] = [
  "no, i meant",
  "that's not what i",
  "you misunderstood",
  "not quite",
  "try again",
  "that's wrong",
  "incorrect",
  "you said you would",
  "you didn't",
  "still not",
  "that didn't work",
];

// ── Calibration state ─────────────────────────────────────────────────────────
// Populated by loadCalibrationOverrides() at plugin startup.
// These are base + RSI-learned deltas; network.json hints stack on top at call time.

let _calibratedProductiveKws: string[] = [...TIER1_KEYWORDS];
let _calibratedMaintenanceKws: string[] = [...TIER2_KEYWORDS];
let _calibratedCorrectionPatterns: string[] = [...CORRECTION_PATTERNS];
let _confidenceParams = {
  base: 0.5,
  per_signal: 0.1,
  max: 0.9,
  ambiguous_no_signals: 0.3,
  ambiguous_tie: 0.4,
};

function _applyDelta(base: string[], add?: string[], remove?: string[]): string[] {
  const removeSet = new Set(remove ?? []);
  return [...base.filter((kw) => !removeSet.has(kw)), ...(add ?? [])];
}

/**
 * Load classifier calibration overrides from {sharedDir}/calibration/classifier.json.
 * Must be called once at plugin startup before any classification occurs.
 * Safe to call if the file is absent — defaults are preserved.
 */
export function loadCalibrationOverrides(sharedDir: string): void {
  const calibPath = path.join(sharedDir, "calibration", "classifier.json");
  let cal: Record<string, any>;
  try {
    const raw = fs.readFileSync(calibPath, "utf8");
    cal = JSON.parse(raw)?.classifier ?? {};
  } catch {
    return; // File absent or unreadable — use base defaults
  }

  _calibratedProductiveKws = _applyDelta(
    TIER1_KEYWORDS,
    cal["productive_keywords_add"],
    cal["productive_keywords_remove"],
  );
  _calibratedMaintenanceKws = _applyDelta(
    TIER2_KEYWORDS,
    cal["maintenance_keywords_add"],
    cal["maintenance_keywords_remove"],
  );
  _calibratedCorrectionPatterns = _applyDelta(
    CORRECTION_PATTERNS,
    cal["correction_patterns_add"],
    cal["correction_patterns_remove"],
  );

  if (cal["confidence_params"] && typeof cal["confidence_params"] === "object") {
    _confidenceParams = { ..._confidenceParams, ...cal["confidence_params"] };
  }
}

/**
 * Return the calibrated CORRECTION_PATTERNS array (base + RSI-learned deltas).
 * Used by TurnObserver so correction detection also benefits from calibration.
 */
export function getCalibratedCorrectionPatterns(): string[] {
  return _calibratedCorrectionPatterns;
}

export function classifyTierByKeywords(
  userMessage: string,
  assistantMessage: string,
  _sessionId: string,
  hints?: { productive_extra?: string[]; maintenance_extra?: string[] }
): SessionClassResult {
  const combined = (userMessage + " " + assistantMessage).toLowerCase();

  // Priority: calibrated base → network.json hints stack on top
  const allProductiveKws = [..._calibratedProductiveKws, ...(hints?.productive_extra ?? [])];
  const allMaintenanceKws = [..._calibratedMaintenanceKws, ...(hints?.maintenance_extra ?? [])];

  const maintenanceHits = allMaintenanceKws.filter((kw) => combined.includes(kw));
  const productiveHits = allProductiveKws.filter((kw) => combined.includes(kw));

  const maintenanceScore = maintenanceHits.length;
  const productiveScore = productiveHits.length;

  const { base, per_signal, max, ambiguous_no_signals, ambiguous_tie } = _confidenceParams;

  if (maintenanceScore === 0 && productiveScore === 0) {
    return { class: "ambiguous", signals: [], confidence: ambiguous_no_signals };
  }

  if (maintenanceScore > productiveScore) {
    const confidence = Math.min(max, base + (maintenanceScore - productiveScore) * per_signal);
    return { class: "maintenance", signals: maintenanceHits.slice(0, 5), confidence };
  }

  if (productiveScore > maintenanceScore) {
    const confidence = Math.min(max, base + (productiveScore - maintenanceScore) * per_signal);
    return { class: "productive", signals: productiveHits.slice(0, 5), confidence };
  }

  // Equal — ambiguous
  return {
    class: "ambiguous",
    signals: [...productiveHits.slice(0, 3), ...maintenanceHits.slice(0, 3)],
    confidence: ambiguous_tie,
  };
}
