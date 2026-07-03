# Principle: Design for the Plex Test

**Status:** load-bearing design principle (not a soft guideline).
**Adopted:** 2026-05-31, consolidating constraints previously scattered across product-vision.md and several specs.

---

## The principle, in three clauses

1. **The audience benchmark is Marcus.** Marcus is the persona at [product-vision.md](product-vision.md#who-evolve-is-for) — a solo professional who installs Plex on a NAS and runs Home Assistant at home. If a primary surface in Evolve is not usable by Marcus without Stack Overflow, LLM lookup, or grep, it is a bug.

2. **No internal jargon in primary surfaces.** Concepts like "RSI", "proposal pipeline", "applier dispatch", "L1/L2", "tier cascade", "signal store", "ConfigPatch", "applier", "generator", "arbiter" are codebase vocabulary, not operator vocabulary. They do not appear in operator-facing chips, alerts, tile copy, wizards, primary settings labels, or marketing copy. Translate them or omit them.

3. **No assumed expertise in primary surfaces.** No CVE numbers without plain-English summaries. No Linux paths, shell syntax, JSON field names, or model identifiers in operator-facing copy. Operator-facing copy means it works on a phone, between other tasks, without context-switching to documentation.

## What is a "primary surface"?

**Primary surfaces** are anything the operator routinely encounters without opting into a deeper view:

- Bot tiles on the dashboard (labels, chips, status, hover state)
- Alert messages in the operator's chat channel
- Toasts, banners, and notifications
- Wizard screens
- The dashboard chrome (nav, page titles, section labels)
- Settings page section labels and primary control labels
- Marketing copy at evolve.ai and gitpages

**Secondary surfaces** can use technical terms when accurate and informative:

- Logs, raw event views, the Signals page detail view
- Advanced / expert-mode settings panels
- The Maintenance page status detail
- API responses and the MCP-tool layer
- Source code, spec docs, internal runbooks

The two-surface model is the lever. We don't ban technical content; we ban it from places where Marcus has no choice but to look at it.

## What this implies in code

Practical translation across the codebase:

### Tile chips and alert text are plain English

Chip labels and details in `packages/analyzer/tile_metrics.py` are written for Marcus, not for the engineer who built the detector. "pushback" is principled; "high_correction_rate exceeded threshold" would not be. Catalog `body_template` entries in `packages/admin/evolve_admin/alerts/catalog.py` follow [operator-message-style.md](operator-message-style.md) — that style guide is the operational expression of this principle for messages.

### Error messages name the failed condition, not the implementation

"Couldn't reach the bot — its gateway is down" is principled; "EACCES on /Users/{bot}/.openclaw/openclaw.json during ConfigPatch apply" is not. The latter belongs in logs, not in a banner.

### Settings labels match the operator's mental model

"Daily spending limit" is principled; "daily_cap_usd" is not. The storage schema is implementation; the label is product. The two routinely diverge — that's the work.

### Operator copy is reviewed against this principle

PRs that touch primary-surface copy should cite this doc. Reviewers (human or agent) check operator-facing strings against the jargon list and the Plex-test bar. The check is "would Marcus understand this on his phone in 5 seconds" — not "is it technically accurate."

## Anti-patterns to grep for

These are violations and should be fixed when found in primary surfaces (logs and code comments are exempt):

- "RSI", "proposal", "applier", "generator", "arbiter", "signal" as user-facing nouns
- "L1", "L2", "L3", "L4", "L5", "L6" as user-facing tier names
- "ConfigPatch", "UpdatePermissionConfig", action-class names in user-facing text
- "tier cascade", "tier-3", "primary tier", "secondary tier" as user-facing concepts
- CVE numbers without a plain-English summary above them
- File paths in toasts and banners (`/Users/<bot>/.openclaw/openclaw.json` in a tile tooltip)
- Raw timestamps without humanization ("1748462280" instead of "2 hours ago")
- Model identifiers in operator copy ("claude-sonnet-4-6" instead of "the bot's main model")
- "Reply 'foo' in the Evolve bot conversation" (already a known offender — see operator-message-style.md)

## What this principle is NOT

- **Not a ban on technical detail in secondary surfaces.** Logs, the Signals detail view, advanced settings, and the Maintenance page can and should use exact field names — operators who go there have opted in.
- **Not a demand for hand-holding.** Marcus is competent. He doesn't need a tutorial; he needs a UI that doesn't make him learn our vocabulary to use it.
- **Not a demand for dumbing down.** Plain English can be precise. "Cost spike: $8.06 today vs $0.95 yesterday" is plain and exact at the same time.
- **Not retroactive at all surfaces simultaneously.** Existing jargon-laden surfaces can be migrated incrementally. New surfaces must be principle-aligned from day one; old surfaces get cleaned up when next touched.

## Why this matters

Evolve's audience is mildly tech-capable individuals and small operators — Marcus, Diana, Carla (see [product-vision.md](product-vision.md#who-evolve-is-for)). The competitive frame is Tailscale, Notion, and Plex — products that an individual can install, configure, and use without a platform team. If primary surfaces require knowledge of Evolve's internals to interpret, we have built a developer tool, not a product for our audience.

This principle is the cheapest insurance against drift toward developer-tool ergonomics. Every contributor (and every agent like Claude Code) cites it by name when reviewing primary surfaces.

## References

- [product-vision.md](product-vision.md#who-evolve-is-for) — Marcus / Diana / Carla persona definitions
- [operator-message-style.md](operator-message-style.md) — operational expression of this principle for chat messages (CI-enforced)
- [principle-alerts-explain-and-remediate.md](principle-alerts-explain-and-remediate.md) — the sibling principle for UI alerts and chips
- [spec-primary-bot-interface-2026-05-14.md](spec-primary-bot-interface-2026-05-14.md) — primary-bot copy constraints
- [spec-surface-aware-help-style-2026-05-22.md](spec-surface-aware-help-style-2026-05-22.md) — formalizes the primary/secondary surface distinction for help text
- `packages/analyzer/tile_metrics.py` — chip labels and details (primary surface)
- `packages/admin/evolve_admin/alerts/catalog.py` — alert body templates (primary surface)
