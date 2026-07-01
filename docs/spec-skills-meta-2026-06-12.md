# META:skills — Skills & plugins substrate (mission + scope + backlog)

**Status:** seeded 2026-06-12. This is the coordinator spec for the `skills`
META aspect — it states the mission, the scope boundary, the invariants, and
the live backlog. It does **not** restate the design of the subsystems it owns;
those live in the inherited corpus (§2) and stay the design source of truth.

---

## 1. Mission

Make the **installation and maintenance of skills and plugins easy and current**
for an Evolve pod. This is the *substrate layer* — the capabilities a bot can
draw on — as distinct from the *application layer* (goal-shaped contracts) that
composes them. See [docs/applications-vs-skills.md](applications-vs-skills.md):
a **skill** is a capability primitive ("send a Slack message", "read Gmail");
an **application** is a contract built from many skills working in concert.

Concretely, `skills` owns:

- **OC plugins** — inventory, baseline/posture, enable/disable/config, the
  install-trust scanner gate, version/exec-policy drift.
- **Skills** — the capability catalog, per-skill setup flows
  ([docs/skills/](skills/)), the substrate audit chip, agentskills.io alignment.
- **MCP servers** — the admin surface + the MCP-install pattern for new
  integrations.
- **The Plugins admin page** (SPA page id `integrations-keys`) — the operator's
  one-stop surface for all of the above, plus its per-row audit/provenance UX.
- **Install/maintenance machinery** — the per-skill install handlers
  (`packages/admin/evolve_admin/skills/*_install.py`), the bundled-plugin and
  MCP-server install shapes, and keeping installed substrate *current*.

## 2. Inherited design corpus (the source of truth — do not duplicate here)

- [docs/applications-vs-skills.md](applications-vs-skills.md) — the skills↔apps framing.
- [docs/spec-plugin-inventory-2026-05-10.md](spec-plugin-inventory-2026-05-10.md) — the inventory layer (stays; its alert model was superseded).
- [docs/spec-plugin-posture-rework-2026-06-06.md](spec-plugin-posture-rework-2026-06-06.md) — posture reoriented around 4 narrow risk categories; inventory-only by default.
- [docs/spec-plugin-install-trust-2026-06-06.md](spec-plugin-install-trust-2026-06-06.md) — install-time trust / signed-bypass for OC's no-allowlist scanner.
- [docs/spec-mcp-administration-2026-05-10.md](spec-mcp-administration-2026-05-10.md) — MCP server admin.
- [docs/spec-permission-posture-2026-05-10.md](spec-permission-posture-2026-05-10.md) + [docs/spec-openclaw-posture-doctor-2026-05-20.md](spec-openclaw-posture-doctor-2026-05-20.md) — posture/doctor.
- [docs/contributing-skills.md](contributing-skills.md) + [docs/skills/*-setup.md](skills/) — per-skill setup + community contribution.
- Memory: `[[project_evolve_substrate_strategy]]` (OpenClaw-first, standards-aligned), `[[obsidian-mcp-install-pattern]]` (the five-piece MCP-skill install shape + the bundled-plugin variant), `[[project_v1_1_substrate_adoption_priority]]`.

## 3. Invariants / guardrails

1. **Skills vs applications stay distinct** — never collapse a capability
   primitive into a goal contract or vice-versa.
2. **MCP-first + standards-aligned** — prefer MCP over OC-proprietary plugin
   APIs for new integrations; design abstractions around agentskills.io + MCP so
   substrate optionality is preserved (`[[project_evolve_substrate_strategy]]`).
3. **Reuse upstream OC, never reimplement** — install scanner, exec-policy,
   isolation are upstream's; Evolve's value is the layer above
   (`[[feedback_dont_reimplement_upstream]]`).
4. **A new integration is audit-first** — 30-second check whether OC ships a
   bundled provider (→ `runway_install.py` bundled-plugin shape) before assuming
   an MCP server is needed (→ `obsidian/notion_install.py` MCP shape).
5. **Plugin posture is inventory-only by default** — only the four narrow risk
   categories fire Signals; a finding on *every* bot of a producer is an Evolve
   bug, not per-bot drift.
6. **Install flow carries the trust/scan gate** — ClawHub community skills have
   ~12% malware rate; safety is the flagship, not an afterthought.
7. **Token-bearing config files are 0600** via /tmp-stage + sudo cp
   (`[[feedback_bot_secret_config_0600_and_cp_mode_semantics]]`); apps inherit the
   bot's LLM stack, never self-credential (`[[feedback_apps_inherit_bot_llm]]`).
8. **Plugins SPA surface** obeys the style guide + both-theme check.
9. **Ownership boundary** — signal-*producer* quality on plugin monitors is
   `reports`' call (review & hand off, don't rebuild); LLM-provider
   catalog/routing is `model-tiers`'. `skills` owns inventory/baseline
   correctness, the Plugins page UX, and the install/maintenance flows.

## 4. Deploy mechanism

Heterogeneous, canary-gated (`pod.release.mode=canary`):

- **Plugins page + `*-admin` routes** → admin-ui kickstart.
- **Inventory/baseline (plugin monitor)** → plugin-monitor daemon kickstart
  (packaged analyzer — watch the stale-module-cache trap).
- **Actual plugin/skill/MCP installs** → per-bot gateway kickstart +
  `sudo evolve-admin deploy <bot>` (TS plugin + openclaw.json / auth-profiles
  writes; canary an affected bot first).

## 5. Backlog

| # | Item | Altitude | State |
|---|---|---|---|
| T1 | **Plugins page loads indefinitely (hanging spinner)** — tactical bug. Page-load `Promise.all` over the subtab loaders never resolves the Plugins subtab. | L0 hygiene | **investigating** (root-cause agent dispatched 2026-06-12) |
| B-future | Install-flow consolidation — extract the shared `_create_mcp_skill_proposal` helper once a 4th MCP skill ships (per `[[obsidian-mcp-install-pattern]]`). | L1 | not started |
| B-future | agentskills.io compliance surfacing — flag installed skills as standard-compliant vs OC-proprietary (substrate-strategy tactical impl #1). | L2 | not started |

The in-flight ledger (live PRs/chips) lives in memory: `[[project_skills_meta_2026_06_12]]`.
