# Evolve Calibration Schema

The calibration layer is how Evolve's RSI loop persists learned improvements across code upgrades. It separates what the community ships (code + defaults) from what a running installation has learned (calibration data).

## The Problem It Solves

When Evolve is upgraded to a new version, the new code replaces the old. Without a calibration layer, any threshold tuning or keyword refinements the local RSI loop produced would be lost. The calibration layer is a set of JSON files that live outside the code tree, in the same shared directory as all other Evolve data, and survive upgrades untouched.

## Three-Tier Architecture

```
Priority (highest → lowest):

1. /Users/Shared/evolve/calibration/*.json   ← RSI-learned, never touched by upgrades
2. network.json (operator overrides)          ← deployment-specific config
3. packages/analyzer/calibration_defaults/   ← community priors, ships with code
```

When a script needs a threshold, it reads the calibration file first. If the local calibration doesn't have a value, it falls back to the community default. If neither has it, the code's hardcoded value is the last resort (these should not exist after full migration).

## Files

All calibration files live in `/Users/Shared/evolve/calibration/`. Each file:
- Contains only the values it overrides (sparse override, not full copy)
- Is written atomically (`.tmp` → rename)
- Has a `schema_version` field for migrations

### `detectors.json`

Thresholds and learned confidence multipliers for all pattern detectors.

```json
{
  "schema_version": 1,
  "detectors": {
    "high_maintenance_ratio": {
      "enabled": true,
      "avg_ratio_threshold": 0.35,
      "consecutive_days_threshold": 3,
      "consecutive_check_window": 5,
      "confidence_base": 0.75,
      "confidence_multiplier": 1.0,
      "outcome_stats": {
        "thumbs_up": 0,
        "thumbs_down": 0,
        "expired": 0
      }
    },
    "api_key_fallback": {
      "enabled": true,
      "confidence_base": 0.92,
      "confidence_multiplier": 1.0
    },
    "declining_resolution_rate": {
      "enabled": true,
      "min_history_days": 7,
      "recent_window_days": 3,
      "min_decline_threshold": 0.15,
      "confidence_base": 0.65,
      "confidence_multiplier": 1.0
    },
    "zero_activity": {
      "enabled": true,
      "no_data_days_threshold": 3,
      "confidence_base": 0.85,
      "confidence_multiplier": 1.0
    },
    "low_satisfaction_application": {
      "enabled": true,
      "satisfaction_threshold": 3,
      "confidence_base": 0.65,
      "confidence_multiplier": 1.0
    },
    "promise_breach": {
      "enabled": true,
      "min_summaries": 3,
      "promise_rate_threshold": 0.40,
      "confidence_base": 0.60,
      "confidence_multiplier": 1.0
    },
    "efficiency_problems": {
      "enabled": true,
      "min_summaries": 5,
      "min_flagged_count": 3,
      "flag_rate_threshold": 0.25,
      "confidence_base": 0.70,
      "confidence_multiplier": 1.0
    },
    "detector_staleness": {
      "enabled": true,
      "rejection_rate_threshold": 0.80,
      "min_proposals_to_evaluate": 5
    }
  }
}
```

**`confidence_multiplier`** is the RSI-learned value. It starts at 1.0 and drifts based on outcome feedback:
- Positive outcome rate > 70% → multiplier nudges up by 0.05 (max 1.50)
- Positive outcome rate < 40% → multiplier nudges down by 0.05 (min 0.30)
- Effective confidence = `min(0.95, confidence_base × confidence_multiplier)`

**`outcome_stats`** is accumulated by `outcome.py` as Pod-admin responds to check-ins. The recalibration itself (calling `calibration.update_confidence_multiplier`) is triggered by `forge_jobs.py` after each forge run, not by `analyze.py` — `analyze.py` only reads the `confidence_multiplier` it finds in the calibration file.

**`enabled`** can be set by the RSI loop (via `detect_detector_staleness`) to disable a persistently miscalibrated detector without a code change.

---

### `classifier.json`

Learned additions and removals for the session tier classifier's keyword lists.

```json
{
  "schema_version": 1,
  "classifier": {
    "productive_keywords_add": [],
    "productive_keywords_remove": [],
    "maintenance_keywords_add": [],
    "maintenance_keywords_remove": [],
    "correction_patterns_add": [],
    "correction_patterns_remove": [],
    "confidence_params": {
      "base": 0.5,
      "per_signal": 0.1,
      "max": 0.9,
      "ambiguous_no_signals": 0.3,
      "ambiguous_tie": 0.4
    }
  }
}
```

The keyword lists are **additive deltas** on top of the community defaults baked into `TierClassifier.ts`. The plugin reads this file at startup and merges the additions/removals before classifying sessions.

**Relationship to `network.json` `classifierHints`:** The `classifierHints.productive_extra` and `classifierHints.maintenance_extra` fields in `network.json` are deployment-specific, operator-set additions (e.g., project names specific to this pod). They are NOT overridden by calibration — they stack on top. Priority order for keywords:
1. Base keywords in `TierClassifier.ts` (community defaults)
2. Calibration adds/removes (RSI-learned)
3. `network.json` classifierHints (deployment-specific, always wins)

---

### `measure.json`

Thresholds for daily metrics status computation.

```json
{
  "schema_version": 1,
  "measure": {
    "status_thresholds": {
      "critical_maintenance_ratio": 0.50,
      "warning_maintenance_ratio": 0.20,
      "critical_api_key_turns": 1
    },
    "unresolved_keywords": [
      "couldn't", "failed", "partial", "gave up",
      "unclear", "not completed", "no resolution"
    ],
    "unresolved_keywords_remove": []
  }
}
```

**`unresolved_keywords`** controls how `measure.py` decides if a session ended without resolution. This can be extended if the RSI loop detects that the classifier is missing domain-specific failure language.

**`unresolved_keywords_remove`** allows removing a keyword that's causing false positives (e.g., "failed" showing up in success messages like "I failed to find any problems").

---

### `outcomes.json`

Timing for post-apply outcome check-ins.

```json
{
  "schema_version": 1,
  "outcomes": {
    "check_in_days": 7,
    "window_days": 3
  }
}
```

**`check_in_days`:** How many days after a proposal is applied before sending the "Did this help?" check-in. 7 days gives time for the change to have an observable effect.

**`window_days`:** How many days to wait for a response before marking the outcome as `expired`. Short enough that the queue doesn't grow unbounded; long enough for a weekend.

---

### `prompts.json`

Evolved LLM prompts for Evolve's internal analysis pipeline. This file is written by the RSI loop when a prompt variant outperforms the current default.

```json
{
  "schema_version": 1,
  "prompts": {
    "behavioral_test_judge": {
      "system": null,
      "version": "1.0",
      "performance": {
        "agreement_rate": null,
        "sample_size": 0
      }
    },
    "manifest_enrichment": {
      "system": null,
      "version": "1.0",
      "performance": null
    }
  }
}
```

When `system` is `null`, the code uses its built-in default prompt. When a non-null value is present, it overrides the built-in. This makes prompt evolution safe: the original is always recoverable by deleting the key.

**`performance`** tracks the measured agreement rate between this prompt's outputs and ground-truth (Pod-admin's judgments in audits). This is what the RSI loop uses to decide whether the evolved prompt is actually better.

---

## Reading Calibration in Code

Use the `CalibrationLoader` from `calibration.py`:

```python
from calibration import CalibrationLoader

cal = CalibrationLoader(shared_dir)

# Read a whole detector config dict:
cfg = cal.detector("high_maintenance_ratio")
threshold = cfg.get("avg_ratio_threshold", 0.35)

# Read one value with dot-notation:
threshold = cal.get("detectors", "detectors.high_maintenance_ratio.avg_ratio_threshold", 0.35)

# Get calibration-adjusted confidence (base × multiplier, capped at 0.95):
confidence = cal.effective_confidence("high_maintenance_ratio")
```

## Writing Calibration (RSI Loop)

The RSI loop writes calibration via the same loader:

```python
# Update a threshold after recalibration:
cal.set("detectors", "detectors.high_maintenance_ratio.avg_ratio_threshold", 0.38)

# Update outcome stats and recompute confidence_multiplier:
cal.set_detector_outcome_stat("high_maintenance_ratio", "thumbs_up", 7)
cal.set_detector_outcome_stat("high_maintenance_ratio", "thumbs_down", 2)
new_multiplier = cal.update_confidence_multiplier("high_maintenance_ratio", 7, 2, 1)
```

All writes are atomic (`.tmp` → rename) and invalidate the in-memory cache.

## Upgrade Contract

When Evolve ships a new version:

1. New code installs → community defaults in `calibration_defaults/` may change
2. `CalibrationLoader` reads local calibration first → local overrides win
3. New parameters in community defaults with no local override → new defaults apply automatically
4. `schema_version` bumps trigger migration functions registered in `calibration.py`
5. No local calibration data is ever deleted or overwritten by an upgrade

This means:
- A new detector's threshold starts at community default, then drifts via RSI
- A removed detector's calibration data persists harmlessly (orphaned keys)
- A changed threshold in community defaults only applies to installations that haven't calibrated it locally

## What Is NOT in Calibration

| Thing | Where it lives | Reason |
|---|---|---|
| Security review rules | `security_rules.json` (immutable) | Cannot be changed by any proposal; intentionally unoverridable |
| Bot roster / ports | `network.json` | Deployment config, not learned |
| Alerts / channel config | `network.json` | Deployment config, not learned |
| Module enable/disable | `network.json` | Operator decision, not learned |
| Raw annotations | `annotations/` | Source data, never calibration output |
| Application manifests | `applications/` | Per-application config, not global tuning |

## TypeScript Plugin Integration

`TierClassifier.ts` needs to read from `calibration/classifier.json` at plugin startup. This is not yet implemented. The planned approach:

1. The plugin's initialization code reads `{sharedDir}/calibration/classifier.json` if present
2. The `_add` lists are merged into the keyword arrays before any classification occurs
3. The `_remove` lists filter out keywords from the base arrays
4. The `confidence_params` override the formula constants if present
5. On plugin restart (gateway restart), the new classifier config takes effect

Until this is implemented, classifier calibration changes must be manually applied to `TierClassifier.ts` source code and deployed as a code update.
