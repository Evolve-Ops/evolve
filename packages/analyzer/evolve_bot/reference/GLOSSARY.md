<!-- Seeded by Evolve from packages/analyzer/evolve_bot/reference/GLOSSARY.md.
     On-demand reference for the primary bot — NOT injected into per-turn
     context. The pod glossary: every tile chip, signal producer, and
     proposal generator, with act-vs-defer guidance.
     Edit the repo file, not this deployed copy — it is overwritten on
     every deploy. -->

## Pod glossary — how to use this file

The glossary content below the divider is regenerated on every deploy
from `packages/analyzer/evolve_bot/glossary.yaml` with your pod's
`network.json::evo_glossary_overrides` applied (the same render
`evolve-admin gen-evo-glossary` produces). It teaches:

- **Tile chips** (Dashboard pills): every chip id, when it fires,
  whether to act or defer.
- **Signal producers** (Alerts page sources): every producer the
  analyzer emits signals under, their signal types, sweep-resolve
  behavior, and act-vs-defer guidance.
- **Proposal generators** (Recommendations page sources): every
  generator that proposes pod changes, their cadence, audience, and
  when to encourage apply vs defer.
- **Severity → urgency cheat sheet** at the end.

When the operator asks about a chip / signal / proposal by name, your
answer comes from this file — read it rather than guessing from the
name. If nothing follows the divider, the deploy-time render failed:
say so instead of improvising definitions.
