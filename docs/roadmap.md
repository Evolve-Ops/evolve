# Evolve — Roadmap

*Last updated: 2026-05-14 (added Primary Bot Interface bundles row, linking spec + live status doc)*

---

## Tools available to future sessions

Before starting a feature that needs multi-turn conversational state,
LLM-driven extraction, or per-user state machines, **check whether the
wizard engine already gives you what you need.** The conversational
machinery shipped over slices 5a–5b8 is now the project's standard
primitive for any "ask the user some things, classify their reply, take
an action" flow. Don't build a parallel stack.

Inventory (`packages/admin/evolve_admin/evo/wizard/`):

- **`state.py`** — atomic per-(bot_id, user_key) state file with status
  lifecycle (in_progress, completed, paused, skipped). Audiences:
  `primary`, `secondary`, `guide_drafter`, `approver`. Add new audiences
  here when the natural finalization shape differs (profile-committing
  vs. not).
- **`phases.py`** — Phase = name + targets + exit_condition + next_phase.
  Engine special-cases phases whose handler owns its own routing
  (CHALLENGE, GUIDE_CONFIRM, GALLERY_RECS, REC_PENDING). Linear chain
  phases just declare `next_phase` and let the engine advance on
  exit_condition.
- **`extractor.py`** — server-side Anthropic call (Haiku, urllib, no SDK
  dep) that pulls structured JSON from a free-form user message against a
  declared `targets` schema. Test seam: `set_extractor(fn)`. Use this for
  any "extract some fields from a chat reply" need.
- **`intent.py`** — same shape as `extractor.py` but for intent
  classification: stage-1 PHRASES/WORDS classifier + stage-2 LLM with
  confidence + snooze-duration hint. Use the
  `_AMBIGUITY_PHRASES`/`_*_PHRASES`/`_*_WORDS` split pattern when you add
  new keyword sets — single-word matches embedded in longer phrases
  ("no" inside "not sure") are the failure mode this guards against.
- **`engine.py`** — `process_turn` is the central routing function;
  start_session / start_guide_draft / start_rec_pending are the
  per-audience entrypoints. Phase-specific handlers (`_handle_challenge`,
  `_handle_guide_confirm`, `_handle_gallery_recs`, `_handle_rec_pending`)
  are the pattern for non-extractor-driven phases.
- **`prompts.py`** — `build(phase_name, extracted, *, context)` is the
  single dispatch; per-phase block functions render the systemAppend the
  bot's LLM consumes. Voice cue: prompts ask the bot to speak naturally,
  NOT to read instructions verbatim — the wizard prompts are agenda
  guidance, not a script.
- **`audit.py`** — `evo.audit.append_event` writes JSONL audit records
  for any state-changing action. Use this for proposal acceptance, claim
  changes, guide saves, etc.
- **Plugin side**: `EvoDispatchClient.wizardTurn(...)` and the
  `wizardSessionId` lifecycle in `TurnObserver.handleBeforeAgentRun`
  already route subsequent user messages through `/api/evo/wizard/turn`
  once a session is started — your new phase gets that for free.

When you ship a new phase, follow the pattern documented in
`docs/spec-evo-wizard-2026-05-05.md` and add tests modeled on
`packages/admin/tests/test_evo_wizard_*.py`.

---

## Architecture (current as of 2026-04-19)

Two-layer pod + security protocol:
1. **Bot users** — admin-bot, team-bot-a, team-bot-b, etc. (do the work)
2. **evolve user** — manages, improves, and audits the pod

Key decisions:
- All Evolve infra jobs run as evolve user (not admin-bot/team-bot-a)
- Admin server runs as evolve user
- No "delegated" comms mode — headless or dedicated gateway only
- POD_CONDUCT.md injected into all bots (shared behavioral contract)
- Security monitoring via `audit.py` (evolve user) + git backup + HMAC signing — no separate Security-bot user
- Security-bot bot concept retired for single-machine pods; revisit if pod goes multi-machine

See: docs/evolve-bot-architecture.md, docs/spec-security-protocol.md

---

## Pillar Status

### 1. Administration
| Feature | Status |
|---|---|
| evolve-admin deploy \<bot\> (plugin only) | ✅ bot-only; infra on evolve user |
| evolve-admin setup evolve-user | ✅ implemented |
| evolve-admin migrate-jobs | ✅ implemented |
| evolve-admin setup --fresh (wizard) | ⚠️ Security-bot step to be replaced with Security Config step (Phase 3a) |
| ocadmin --json mode | ✅ implemented |
| Admin UI: Key Management | ✅ wired to /api/admin/keys |
| Admin UI: Model Management | ✅ wired to /api/admin/models (AI Optimization page) |
| Admin UI: Usage Analytics from ocadmin | ✅ wired to /api/admin/usage (Keys & Usage page) |
| POD_CONDUCT.md injection in deploy | ✅ workspace copy + AGENTS.md reference |

### 2. Monitoring
| Feature | Status |
|---|---|
| Gateway liveness | ✅ |
| Session activity metrics | ✅ |
| Pod health score | ✅ |
| Alert delivery | ✅ |
| Historical trend charts | ✅ |
| Real-time metrics via OC plugin | ✅ |

### 3. Integrations
| Feature | Status |
|---|---|
| Channel status | ✅ |
| API key health | ✅ |
| OAuth freshness | ✅ |
| Data source: reads from openclaw.json plugins | ✅ |

### 4. Cost Management
| Feature | Status |
|---|---|
| Daily/monthly spend display | ✅ |
| MAX vs API fallback monitoring | ✅ |
| Spend alerts | ✅ |
| Historical trends | ✅ |
| Cost efficiency scoring + spend controls | ✅ implemented (PR #9) |

### 5. AI Optimization
| Feature | Status |
|---|---|
| Model config display | ✅ |
| Model management via ocadmin | ✅ edit catalog via UI |
| Routing rules display | ✅ |
| Compaction settings | ✅ |
| Model routing tiers + fallback (v2) | ⚠️ sections 1–3, 5-static, 7 implemented; Phase 2 deferred |

### 6. Maintenance
| Feature | Status |
|---|---|
| Cron job status | ✅ |
| heal.py self-healing | ✅ runs as evolve user (migrate-jobs installs it) |
| measure.py to evolve user | ✅ migrate-jobs installs as evolve (was per-bot) |
| apply.py per-bot by design | ✅ installed by `evolve-admin deploy <bot>` — intentionally runs as bot user |
| Maintenance log | ✅ |
| Session management UI (list + kill) | ❌ not yet built — spec: docs/spec-session-management-ui-2026-04-19.md |
| Session sweeper rules (auto-close stale) | ❌ Phase 2 — see spec |

### 7. Security
| Feature | Status |
|---|---|
| Evolve security audit (OC audit) | ✅ Run Audit button working |
| Config baseline monitoring | ✅ |
| Security scoring | ✅ |
| POD_CONDUCT.md admin UI page | ✅ Pod Conduct nav page |
| Security Protocol v2 spec | ✅ docs/spec-security-protocol.md |
| Proposal pipeline integrity signing (HMAC) | ✅ Phase 3a — evolve_config.py helpers + signed at write |
| Git backup infrastructure (backup.py) | ✅ Phase 3a — nightly cron, SSH deploy keys, drift-ready |
| Security config step in setup wizard (replaces Security-bot step) | ✅ Phase 3a |
| Drift detection in heal.py | ✅ Phase 3b — git-based, cross-refs apply-results |
| HMAC verification in review.py + apply.py | ✅ Phase 3b — quarantine dir + alerts |
| Dashboard: backup freshness, drift status, quarantine UI | ✅ Phase 3c — /api/security/* endpoints |
| audit.py (identity hashes, config, machine checks) | ✅ Phase 3b — every 15min via launchd |
| Dashboard: identity hash status, machine security checks | ✅ Phase 3c — /api/security/identity-status + machine-status |
| Security-bot independent auditor | 🗄️ retired — replaced by Security Protocol v2 |
| Security-critical proposal class | 🗄️ deferred — revisit for multi-machine pod |

### 8. Applications (Application Development)
| Feature | Status |
|---|---|
| Application manifest schema (RSI-first) | ✅ v6: 4-section RSI core + file layers, skills index, cron delivery intent |
| LLM-enriched manifest generation | ✅ during application scan |
| Structured manifest UI (viewer) | ✅ |
| Editable manifest UI | ✅ |
| PUT endpoint for manifest saves | ✅ |
| Test case runner | ❌ not yet built |
| Application name truncation fix | ✅ fixed |
| Remove meaningless badges (core/feature/optional) | ✅ removed |

### 9. Self-Improvement — legacy v1 pipeline
| Feature | Status |
|---|---|
| 12 pattern detectors | ✅ |
| Proposal generation (analyze.py) | ✅ |
| Proposal review UI (Self-Improvement page) | ✅ |
| POD_CONDUCT.md amendment flow | ❌ not yet |
| Proposal pre-validation (Sandbox) | ❌ deferred — see RSI v2 verify daemon instead |

### 9b. Self-Improvement — Better Engine RSI v2 (L1-L6 shipped 2026-04-19)
| Feature | Status |
|---|---|
| L1 foundation — Proposal v2 schema, arbiter state machine, ingest, dedup, routing, rate-limit, referee primitives | ✅ |
| L2 verify daemon — claim resolution, fallback dispatch (revert / flag / escalate), metric resolvers | ✅ |
| L2 Sysadmin Watchdog — gateways, launchd, plugin, config, permissions, ACLs | ✅ |
| L3 Budget Hawk (cost guardian) | ✅ |
| L3 Security Warden (safety guardian with annotation capability) | ✅ |
| L3 Observation tuples (noun × verb × mood × engagement extraction) | ✅ |
| L4 Adjacency Explorer (extend_same_cell / adjacent_noun / adjacent_verb / chain_completion) | ✅ |
| L4 Profile skeleton + per-bot dimension weights | ✅ |
| L4 Scoring formula in production (urgency × dim_weight × authority + tiebreak) | ✅ |
| L5 Referee — ranking, conflict annotation, weekly rate-limit | ✅ |
| L5 Gap Filler (looks for verb/noun cells the pod doesn't cover) | ✅ |
| L5 Profile Inferrer + Weight Inferrer (confirmation-gated updates) | ✅ |
| L6 Persona Tuner (voice_fit proposals) | ✅ |
| L6 Deprecator (stale app detection) | ✅ |
| L6 Efficiency Hawk (turns/session ✅; cron/heartbeat dominance ✅; tier misrouting ⏳ overlaps budget_hawk; repetitive clarifying-questions ⏳ deferred) | ✅ partial |
| L6 Evolve Watchdog (8 meta-health detectors) | ✅ |
| L6 Calibration snapshots (user / generator / signal targets) | ✅ |
| L6 Intra-dimension competition (registry scheduler, weight reallocation) | ✅ |
| Arbiter bridge — reads v2 proposals, materializes Recommendations for existing UI + evo keyword | ✅ |
| Admin UI Phase 1 — Proposals page + Profile page + rate-limit banner + verify-status + conflict grouping | ✅ |
| Admin UI Phase 2 — Generators page (track record, authority, charter inspector, pause/resume) + scoring-breakdown popover | ✅ |
| Admin UI Phase 3 — Meta-health page (watchdog events + snapshots + observation browser) | ✅ |
| Admin UI Phase 4 — evo keyword polish (dimension word, adjacency framing, guardian concerns) | ✅ |
| Area-dashboard "related proposals" strips on Cost Measures, Security, Maintenance | ✅ |
| Test coverage: 507 analyzer + 320 admin (total 827) | ✅ |
| Signal-target calibration rollback (UI + storage layer) | ⚠️ 501 placeholder; user + generator rollback live |
| POD_CONDUCT.md amendment flow as a v2 proposal class | ❌ deferred |
| Conversational approval (NL intent parsing on the evo keyword surface) | ✅ slice 5b8 days 1+2 shipped — folded into the wizard engine as `PHASE_REC_PENDING` with a two-stage intent classifier (`evo/wizard/intent.py`), config knobs (`conversational_approval.{enabled, llm_intent_parse_enabled, confidence_threshold, default_snooze_days, pending_expiry_minutes, push_preamble_enabled}`), session TTL on idle approver-audience sessions, snooze-duration extraction wired through `BetterEngine.snooze(days_override=…)`, voice cues from SOUL.md + bot RSI profile, and push-mode scaffolding behind `start_push_preamble`. Plugin call site for push delivery deferred to a separate spec. See `docs/spec-better-engine-conversational-approval-2026-04-18.md` (Implementation status section). |
| Security Warden completion (see spec-security-warden-completion-2026-04-18.md) | ⚠️ 1 of 3 detectors shipped — prompt-injection scanner live: production Haiku verifier (`wire_default_verifier()`), multilingual patterns (EN + ES/FR/PT/IT/DE), per-run + cross-session `do_not_reflag` (auto-promotes after 3 dismissals; arbiter `/dismiss` hook + `evolve-admin warden suppress|unsuppress|suppressions` CLI), 48h lookback, adversarial corpus (30 attacks/28 legit), mtime-pruned metric resolver, `security.injection_events_per_week`, `injection_scanner_respects_consent` charter invariant, and `evolve-admin migrate-generator-records` deploy helper. Exfiltration (§5) still deferred — needs role + destination tagging in capture pipeline first. Machine-level probe daemon (§6) deferred. |

### 10. Continuity Engine
| Feature | Status |
|---|---|
| Task extraction from transcripts | ✅ |
| Task queue | ✅ |
| Recurring tasks | ✅ |
| Session start surfacing | ✅ AGENTS.md injection + session_start hook in TurnObserver |
| Approval flow UI | ✅ needs_approval tasks show Approve/Reject buttons; POST /api/tasks/:id/approve |

### Primary Bot Interface (multi-bundle, in progress)

Spec: [`spec-primary-bot-interface-2026-05-14.md`](spec-primary-bot-interface-2026-05-14.md). Live status + pickup notes: [`primary-bot-interface-status.md`](primary-bot-interface-status.md).

| Bundle | Scope | Status |
|---|---|---|
| 1 — Intake | `evo bug`/`feature`/`intake` chat surface + CLI + Flask routes + GitHub promotion via keystore PAT | ✅ #1101 merged |
| 2A — Help retrieval (Python) | BM25 over `docs/help/*` + curated operator docs; `/api/evo/help/{search,read}`; `evolve-admin help-index` CLI; wired into `deploy_shared_dir` | 🟡 #1106 open |
| 2B — Bot integration | `session_surface.py` primary-block + help sidebar; TS plugin tool registration for `evolve_help_search` / `evolve_help_read` / `submit_intake` gated on `role==primary` | ⬜ not started |
| 3 — Pod-state + Admin UI | Refactor mcp_bridge tools into `evolve_admin.pod_state`; new `/api/primary/state/*` routes (`pod_status`, `list_signals`, `list_proposals`, `describe_bot`, …); admin-UI Intake sub-page under Alerts | ⬜ not started |

After 2B lands, admin can ask the primary bot free-text Evolve questions and get grounded answers ("what is an audit?") instead of hallucinations. After 3 lands, the bot has tool-calls for live pod state and admin has an Intake UI to triage captures.

See `primary-bot-interface-status.md` for known follow-ups, second-pass review findings, and pickup hints for whichever session picks up next.

---

## Immediate Priority: deploy + validate RSI v2 in production

L1-L6 RSI backend + admin-UI Phase 1-4 landed on branch `claude/hardcore-gauss-7da6ed`
(2026-04-19). The next pass is operational validation:

1. **Commit + merge/rebase** against current main; push.
2. **Deploy** to the Mac mini via the existing `git pull + launchctl kickstart` flow.
3. **Wire the scheduled jobs** — the bridge (`proposal_reader`) is already registered
   in `better_engine_refresh.py`; confirm the refresh runs every 15 minutes and that
   the verify daemon, calibration snapshots, and Watchdog generator cadences land
   on launchd.
4. **Observe** for a day: does the Proposals page populate? Do any generators emit
   proposals? Does the verify daemon transition anything?
5. **Calibrate** generator thresholds based on real data. Several are likely too
   sensitive or too quiet at their default settings; this is expected alpha-era work.

---

## Phase Plan

### Phase 1: Architecture completion ✅
- Code cleanup complete
- Fresh reinstall complete
- All pillars verified with evolve user architecture

### Phase 2: Missing features ✅
- ~~Session start surfacing for Continuity Engine~~ ✅ done (AGENTS.md injection + session_start hook)
- ~~Lifecycle management UI~~ ✅ done (upgrade/uninstall modals + CLI)
- ~~Admin Service Management~~ ✅ done (`evolve-admin service install/status/restart` + UI panel)
- ~~Cost measures pillar~~ ✅ done (PR #9)

### Phase 3: Security Protocol v2

Security-bot bot retired. Security delivered via four integrated layers running as the evolve user.
Full spec: docs/spec-security-protocol.md

**Phase 3a — Foundation** ✅:
- **HMAC signing** — evolve_config.py helpers + key generation in setup_evolve_user(); analyze.py, cost.py, heal.py sign proposals; review.py adds review_stamp; validate.py signs forge results
- **backup.py** — nightly git backup, SSH deploy key generation per bot in deploy_bot(), launchd at 02:00
- **Setup wizard security config step** — replaces Security-bot step; collects security Telegram token + GitHub backup repo URL; stores token in keystore/security-alert-token

**Phase 3b — Verification** ✅ (partial):
- **HMAC verification in review.py** — invalid evolve_sig → proposals/quarantine/ + CRITICAL alert ✅
- **HMAC verification in apply.py** — verifies evolve_sig + review_stamp.sig + forge_sig; quarantines on failure ✅
- **Drift detection in heal.py** — git show HEAD vs live openclaw.json; cross-refs apply-results; CRITICAL alert + incident + investigation proposal on unexplained diff ✅
- **audit.py** — identity hashes (SOUL.md/AGENTS.md/HEARTBEAT.md vs git backup), config audit (gateway.bind, exec allowlist, plugins, sudoers hash), machine audit (firewall, SSH, user accounts, ports, OC binary mtime), cost audit, proposal volume audit ✅

**Phase 3c — Dashboard** ✅:
- /api/security/backup-status — per-bot last backup timestamp + stale flag ✅
- /api/security/drift-status — per-bot drift incident count (24h) ✅
- /api/security/quarantine — quarantined proposal list ✅
- POST /api/security/alert-channel/test — test security Telegram token ✅
- /api/security/identity-status — identity hash results from audit.log ✅
- /api/security/machine-status — machine security check results from audit.log ✅

### Phase 4: Intelligence loop
- First proposal generation run
- A/B testing: apply proposal to Team-bot-a, hold Admin-bot as control

### Phase 4: Continuity Engine
- Task extraction dry run (review quality before enabling)
- Approval flow implementation
- Enable for pre-approved recurring tasks first

### Phase 5: External Integrations
- **MCP Bridge / Claude Desktop Integration** (spec: docs/spec-claude-integration-2026-04-11.md)
  - Evolve MCP server running on pod host (evolve user, port 5051)
  - Claude Desktop / Claude Desktop Dispatch connect via Tailscale
  - Read tools: get_context, get_tasks, get_proposals, get_pod_status, get_metrics
  - Write tools: add_note, update_task, create_handoff
  - Setup wizard step: configure Tailscale hostname + primary context bot
  - Admin UI: MCP Bridge status panel, Claude Desktop config snippet
  - Tailscale added to pre-install checklist as strongly recommended
  - Status: Final draft — ready to build

---

## Anthropic Billing Change (April 4, 2026)

> ⚠️ **Transition credit expires April 17, 2026 — 5 days from now. After that, all usage is pay-per-use API billing.**

Anthropic ended MAX subscription access for third-party tools including OpenClaw.
All OC instances must now use API keys (pay-per-use) or Anthropic's new "extra usage" billing.

**Immediate impacts on Evolve:**
- Cost Management pillar becomes more critical — real money now
- Model routing (tier1/tier2/tier3) becomes a cost-saving tool, not just a quality tool
- Analysis scripts (Haiku calls in analyze.py, classifier, manifest generation) now cost real money
- ocadmin integration for key management becomes essential (not optional)

**Evolve response:**
- Cost alerts already built — ensure they work with API-key billing
- Cost efficiency scoring + spend controls: ✅ built and deployed (PR #9)
- Haiku usage in analysis scripts: already cheap (~$0.001/call), acceptable
- Weekly review: consider reducing LLM calls in analysis pipeline

---

## Known Observability Gaps

### Per-call embedding-provider observability

**What's missing:** evolve has no visibility into individual *successful*
memory_search embedding calls. We see failures and lifetime totals, but
nothing in between.

**What we do have** (in case the gap looks bigger than it is):
- **Failure rate + classification per provider per bot** — `embedding_monitor`
  tails each bot's `gateway.err.log` hourly and emits Signals
  (`provider_failing`, `rate_limit_storm`) with HTTP status, error class
  (auth/quota/provider_error/bad_request), and the memory-op reason
  (`session-start` / `search` / `watch`).
- **Cumulative memory-dir size** as a rough lifetime-of-bot proxy for
  total embedding work (security-bot 260MB vs evolve 68KB tells you who uses
  memory_search heavily, even without per-call data).
- **Provider-switch events** via `[reload] config change detected
  (agents.defaults.memorySearch)` lines — confirms when a chain change
  was actually picked up.

**What we don't have:**
- Per-call success counts (every successful embedding call is silent —
  no log line, no cost_event)
- Token volumes per call → no cost computation possible
- Latency per provider
- Activity rate over time

**Root cause:** OpenClaw emits `cost_event` records for chat completions
only; embedding API calls produce no analogous record. `EMBEDDING_PRICING`
in [packages/analyzer/embeddings.py](packages/analyzer/embeddings.py)
is ready to consume them the moment OC starts emitting.

**Why we're not working around it via a plugin:** OC's plugin SDK does
expose `api.registerMemoryEmbeddingProvider(adapter)` — technically a
wrapping adapter could observe each call and emit a cost event. But the
bug surface is asymmetric: a wrapper bug breaks memory_search itself
(operational system), which is wrong-trade for analytics on a cost
surface that's typically <5% of LLM spend. Five wrappers (one per
provider) would also accumulate maintenance debt against OC's adapter
contract.

**Right fix:** ~5-line addition to OC's embedding adapter to emit a
`cost_event` record alongside the API call. Whenever someone is next
in the OC repo for another reason.

**Cheap mitigation if the gap bites before that:**
- Daily cron running `openclaw memory status --deep` per bot, with
  chunk-count delta persisted, gives a coarse "memory indexed N
  documents today" signal without OC changes (~30 LOC).
- Memory-dir-size growth rate over time as a usage-page activity metric
  (~30 LOC, even cheaper, less precise).

Don't build either until the gap actually matters. Operationally we're
covered (`embedding_monitor` catches the failure mode that prompted the
whole feature); analytically we have a blind spot we accept.

---

## Deliberately Not Building Yet

| Item | Reason |
|---|---|
| Multi-machine networking | Tailscale + MCP bridge covers the real use case |
| Budget hard limits (auto enforcement) | Spend caps + enforcement flags built; auto-enforce requires operational experience first |
| Model latency tracking | Measure after routing is running |
| More pattern detectors | Calibrate 12 existing ones first |
| Delegated comms mode | Explicitly rejected — see product-vision.md |
| Embedding cost / per-call activity tracking | OC emits `cost_event` for chat only; wrapper plugin too risky for <5% of spend — see Known Observability Gaps above |
