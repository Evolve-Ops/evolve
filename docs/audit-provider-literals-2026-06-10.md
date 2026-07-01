# Audit: hardcoded provider/model-name literals — 2026-06-10

**Principle audited:** provider/model names may live in exactly three places —
(a) catalog/index **data**, (b) provider **adapters**, (c) **tests/fixtures**.
Zero provider/model literals in **logic**: routing, availability, degradation,
UI strings/templates, conditionals keyed on a provider literal, fallback
defaults like `provider = "anthropic"`.

References: [principle-llm-provider-agnostic.md](principle-llm-provider-agnostic.md),
[spec-model-rungs-and-roles-2026-06-09.md](spec-model-rungs-and-roles-2026-06-09.md)
(Addendum 2: the default catalog ships in code; the planned
`DEFAULT_MODEL_CATALOG` is **not in the repo yet** — it lands with Phase 6).
A CI guard for new violations is planned as part of Phase 6b.

## Method

Sweep: `rg -n '"anthropic|anthropic/|claude-|"openai|gpt-|"google|gemini-|grok-'
packages/ --type py --type ts -g '!*test*' -g '!*node_modules*'`, plus a
case-insensitive sweep of `packages/admin/evolve_admin/web/static/js` +
`index.html`, plus a targeted `else "anthropic" | or "anthropic" | ?? 'anthropic'`
pattern sweep. Every hit was classified with surrounding-code context; every
claimed violation was then re-verified by hand against the file before being
listed here. Line numbers were correct at audit time (branch
`claude/jovial-snyder-445988`).

## Inventory counts

| Scope | Hits |
|---|---|
| `packages/` py+ts, non-test | 789 (≈130 files) |
| `web/static/js` + `index.html` | 97 |
| Tests/fixtures (bucket c, legitimate wholesale) | 2,306 |

Bucket rollup for the 886 non-test hits (a/b/n-a counts are sweep-level
approximations; the (d) and (e) lists below are exact and individually
verified):

| Bucket | ≈ Hits | Notes |
|---|---|---|
| (a) catalog/index data | ≈ 520 | pricing tables, tier data, option lists, docstrings/comments |
| (b) provider adapters | ≈ 90 | Anthropic Admin API, auth-profile id shapes, key formats, endpoint dispatch |
| n/a — non-LLM integration | ≈ 240 | Google **Workspace** OAuth/Gmail/Calendar literals matched by the `"google` pattern; not LLM-provider hits |
| (d) logic violations | 12 sites | listed exactly below |
| (e) ambiguous | 24 sites | listed exactly below |

---

## (d) Violations — FIXED in this PR

The one cluster that was unambiguous **and** behavior-preserving to fix:
bare-model-id → presumed-`"anthropic"` provider derivation in the judge
provider-diversity validation.

Four `_provider_of` implementations exist (`model_registry.py:171`, admin
`model_catalog.py:115`, `arbiter/appliers/model_catalog.py:65`, plugin
`ModelRouter.ts:830`) and all return **None/null for bare ids**. The two
sites below were the outliers that presumed `"anthropic"` — and the docstring
claim that this "matches ModelRouter._providerOf" was factually wrong, meaning
the Python validation was **stricter than the runtime resolver it mirrors**
(it could reject a judge mapping the router would happily resolve).

1. **`packages/analyzer/arbiter/appliers/adopt_model.py:103`** —
   `_provider_of` returned `"anthropic"` for bare ids; and `:132`
   `std_provider = … else "anthropic"` presumed Anthropic when the standard
   rung is empty. **Fixed:** None convention, matching the three sibling
   helpers and `ModelRouter._providerOf`.
2. **`packages/admin/evolve_admin/cli.py:7189,7193`** (map-role judge check,
   the check adopt_model explicitly mirrors) — same two fallbacks.
   **Fixed:** same None convention, keeping the mirror in sync.

**Edge-case behavior delta (intentional, runtime-aligning):** model ids in
`models.rungs` are canonically qualified (`_qualified()` re-derives
`provider/model` on every adoption; the CLI writes qualified ids), so real
configs see no change. In the synthetic edge cases — a bare id hand-edited
into a rung, or an empty standard rung — validation now accepts exactly what
`ModelRouter._resolveJudgeModel` would resolve at runtime, instead of
rejecting based on a presumed Anthropic provider. Pinned by a new test
(`test_apply_judge_diversity_presumes_no_provider_for_bare_ids`).

## (d) Violations — behavior-affecting, FIXED with operator sign-off (2026-06-10)

These three were initially documented as proposals only; the operator
approved them and they shipped on this same branch.

3. **`packages/admin/evolve_admin/cli.py`** (`provision-bot`) —
   `@click.option("--auth-choice", default="anthropic", …)`, the literal
   default-arg anti-pattern named in the principle doc. **Fixed:**
   `default=None`; the one sanctioned inference is the legacy
   `--anthropic-api-key` flag (the provider is encoded in the flag name the
   operator explicitly used — same shim shape as `wizard_routes.py:554–557`);
   otherwise the command errors *before any state is created*, listing the
   valid choices from `provisioning.AUTH_CHOICE_TO_KEY_FLAG`, unless
   `--no-onboard`. The backend (`provision_bot::auth_choice=None`, "NO
   default") was already principled — the CLI flag was the last presumption.
   Covered by `test_provision_bot_cli_auth_choice.py` (4 tests).
4. **`packages/admin/evolve_admin/wizard.py`** — terminal bot-creation
   wizard provider menu defaulted to choice "1" (Anthropic), so
   Enter-through selected a provider — the exact "click-through must NOT
   result in a provider being chosen" violation the principle doc defines;
   the "Other" branch even defaulted the free-text provider name to
   `"anthropic"`. **Fixed:** no default; the menu loops until an explicit
   1/2/3 choice, and "Other" requires a non-empty provider name. The
   "[1] Anthropic (recommended)" label stays — recommending is allowed,
   preselecting was the violation.
5. **`packages/analyzer/generators/efficiency_hawk/signal_proposals.py`** —
   heartbeat-cost proposal copy hardcoded
   `"model": "anthropic/claude-haiku-4-5"` in the suggested openclaw.json
   override; on a non-Anthropic pod the proposal would instruct the operator
   to set an Anthropic model. **Fixed:** new `_cheap_tier_model()` resolves
   tier3 via `models.resolve_tier` (the established resolver pattern from
   `arbiter/refine.py` / `bot_forge.py`) and the copy interpolates the
   result; if resolution is unavailable the copy degrades to a placeholder
   rather than presuming a provider. Prose "Haiku"-isms generalized to
   "cheap tier". Output-identical on default/Anthropic pods (verified by
   smoke render: snippet still says `anthropic/claude-haiku-4-5`, now
   sourced from tier data).

## (d)-adjacent — OWNED BY PHASE 6b (inventoried, fixes deferred to that effort)

- **`packages/plugin/src/observer/ModelRouter.ts`** — 10 sweep hits, all
  comments/docstring examples; `_providerOf` itself is already
  None-convention. No live violation found, but the file is mid-rework.
- **`packages/analyzer/model_discovery.py`** (33 hits) — availability/known-set
  logic; family heuristics and listing-endpoint adapters live here. Inventory
  only.
- **`packages/analyzer/primary_bot.py`** (10 hits) — merge/resolution code;
  the hits found are auth-profile key-shape detection (bucket b). Inventory
  only.
- **`packages/admin/evolve_admin/deploy.py:73`** + **`packages/plugin/src/config.ts:126`**
  — `classifierModel: "anthropic/claude-haiku-4-5"` plugin-config default,
  mirrored on both sides. Whether the classifier default should derive from
  the (Phase 6) default catalog / fast role is a router-config question —
  align with the Phase 6b outcome.

## (e) Ambiguous — needs human judgment

**The Anthropic-only infra-LLM cluster.** Evolve's own infrastructure LLM
calls go through the Anthropic SDK / Messages API directly, with a
documented "resolve through tiers, literal only as broken-config fallback"
pattern. Each carries a bare-Haiku/Sonnet fallback literal and (in some) a
`startswith("anthropic/")` capability guard. Individually these read as
adapter code for an Anthropic-only call path; collectively they encode a
product posture — **Evolve's own brain requires Anthropic credentials** —
that deserves an explicit decision rather than 14 scattered literals:

- `packages/analyzer/arbiter/refine.py:47` (+ guard `:92–98`)
- `packages/analyzer/proposal_synthesizer/synthesizer.py:81,84` (+ guard `:125–133`)
- `packages/analyzer/observations/llm_extractor.py:152`
- `packages/admin/evolve_admin/applications/bot_forge.py:69`
- `packages/admin/evolve_admin/applications/forge_engine.py:186–187` (+ guard `:484`)
- `packages/admin/evolve_admin/applications/coherence_c3_dispatcher.py:77` (+ guard `:203`)
- `packages/admin/evolve_admin/applications/reviewer.py:40`
- `packages/admin/evolve_admin/intake/classifier.py:44`
- `packages/admin/evolve_admin/web/home_chat.py:71–74` (whole module is an Anthropic Messages adapter; operator override `network.json::home_chat.model` exists)
- `packages/admin/evolve_admin/evo/inspector.py:302–305` (env-overridable)
- `packages/admin/evolve_admin/evo/wizard/extractor.py:38–42` (env-overridable)
- `packages/admin/evolve_admin/evo/wizard/intent.py:267–273` (env-overridable)
- `packages/admin/evolve_admin/web/routes_arbiter.py:1286,1303` (accurate error messages for the Anthropic-only refine path)
- `packages/admin/evolve_admin/evo/arbiter_bridge.py:533`, `evo/tools/action_proposal_refine.py:139` (same error-message class)

**Question for the operator:** is "infra LLM = Anthropic-only" acceptable
product posture for v1 (then these are bucket-b adapters and the literals are
their documented broken-config fallbacks — done), or does the
provider-agnostic principle extend to Evolve's own infra calls (then the fix
is one shared per-provider dispatch seam, a design effort — note the
help-agent in `server.py:8699+` already dispatches per-provider and could be
the template)?

**Individual ambiguous sites:**

- `packages/admin/evolve_admin/web/routes_oc.py:2504` — health check infers
  `"anthropic"` for bare primary-model ids before checking key presence.
  Same shape as the fixed cluster, but the id comes from **openclaw.json**,
  whose bare-id semantics are OC's (external substrate) — if OC itself
  treats bare ids as Anthropic, this is adapter knowledge. Q: what does OC
  do with a bare model id?
- `packages/admin/evolve_admin/web/server.py:8675` — help-agent picks the
  first tier3 model with a credentialed provider; bare → `"anthropic"`.
  Defensive-only (tier registry ids are qualified) and works because the
  Anthropic API accepts bare claude ids. Also `:8659` broken-config fallback
  list `["anthropic/claude-haiku-4-5"]`.
- `packages/admin/evolve_admin/setup_wizard.py:489,509` +
  `packages/admin/evolve_admin/cli.py:358–365` (`_OC_JSON_EVOLVE`) — the
  evolve service-bot's seed openclaw.json pins
  `anthropic/claude-sonnet-4-6` / haiku classifier regardless of which
  providers the operator credentialed. Q: should the seed derive from
  `models.derive_default_tiers(credentialed_providers)` (which exists for
  exactly this)? A fresh install with only OpenAI keys currently gets a
  non-functional evolve bot.
- `packages/admin/evolve_admin/setup_wizard.py:328` — the no-existing-keys
  fallback prompt only offers an **Anthropic** key entry (skippable, labeled).
  Q: should it ask for provider first?
- `packages/admin/evolve_admin/web/wizard_routes.py:554–557` — legacy
  `anthropic_api_key` body field implies `auth_choice = "anthropic"`. The
  provider is encoded in the field name the caller used, so this is a
  reasonable legacy shim — but it was given "one deprecation cycle"
  (comment); Q: has that cycle elapsed?
- `packages/admin/evolve_admin/web/routes_bot_config.py:222–274,725–761`
  (12 sites) — `.replace("anthropic/", "")` display-normalization strips
  only the Anthropic prefix (non-Anthropic ids keep theirs), and one
  **equality comparison** (`:746`) normalizes both sides the same way.
  Recommend a shared `display_model_id()` helper; not changed here because
  the comparison's matching behavior for non-Anthropic ids is load-bearing
  and unverified.
- `packages/admin/evolve_admin/applications/forge_cost_guard.py:143,296` —
  cost projection falls back to `_PROVIDER_FALLBACK["anthropic"]` pricing for
  unspecified models (documented: intentional no-op for non-Anthropic
  dispatches) and labels the projection `"anthropic/<unspecified-default>"`.
  Q: keep as documented heuristic, or read the bot's actual primary model?
- `packages/admin/evolve_admin/ocadmin.py:1852–1856` — CLI display color key
  special-cases `provider == "anthropic" and auth_mode == "api_key"`
  (fallback is `"unknown"`, not anthropic). Display-only; Anthropic was the
  only provider with the api-key-vs-subscription distinction.
- `packages/analyzer/cost_opt_tiles.py:71–91` — `_MODEL_TIER_BUCKETS` static
  family→tier substring buckets for tile coloring. Q: compute from the
  registry/catalog at import time, or accept manual sync as a display-only
  approximation?
- `packages/analyzer/analyze.py:169` — `if provider == "anthropic"` selects
  Anthropic-specific advisory copy (the post-MAX billing explanation) with a
  generic else branch; provider falls back to `"unknown"`. Provider-keyed
  advisory *content*, not routing — fine, flagged only because it's a literal
  equality check (a dict keyed by provider would be the cleaner shape).

## Notable legitimate clusters (so future audits don't re-litigate)

- **(a) data:** `usage_analytics.py` (pricing tables, 63 hits), `models.py`
  (`_KNOWN_LLM_PROVIDERS`, `_PROVIDER_PICKS`, `DEFAULT_TIERS` — the blessed
  extensible structures), `model_registry.py`, `embeddings.py` (provider
  registry), `oc_model.py`, `cost_*` modules, web `probes/__init__.py`
  (provider option lists), `index.html` provider dropdown (placeholder
  default, "deep integration" label = recommendation, principle-compliant),
  plugin TS comments/pricing keys.
- **(b) adapters:** `anthropic_admin.py` / `anthropic_admin_ingest.py`
  (Anthropic Admin API), `security_warden/redact.py` (`sk-ant-` key shapes),
  `generators/cache_ttl_tuner/applicability.py:62–65` (`cacheRetention` is an
  Anthropic-specific OC param — documented), `cost_root_cause_correlator/
  correlations.py:127` (mirrors the materializer's Anthropic fan-out
  semantics exactly, per comment), `oc_keys.py`, `provisioning.py`
  (`AUTH_CHOICE_TO_KEY_FLAG` — the canonical mapping), `server.py:8699+`
  per-provider endpoint dispatch, `wizard_routes.py` profile-provider maps.
- **n/a:** every `"google`/`googleapis` hit in `skills/google_*`,
  `oauth/providers/*`, `google_auth.py`, `google_preflight.py`,
  `mcp_bridge/google_tools.py`, `wizard_google_*` — Google **Workspace**
  integration, not Gemini-the-LLM.

## Disposition summary

| Disposition | Count |
|---|---|
| Fixed in this PR (behavior-preserving) | 2 sites (4 literals): adopt_model.py, cli.py judge check |
| Fixed in this PR (behavior-affecting, operator-approved) | 3: cli.py `--auth-choice` default, wizard.py provider menu default, efficiency_hawk proposal copy |
| Owned by Phase 6b | ModelRouter.ts, model_discovery.py, primary_bot.py, classifierModel default (deploy.py + config.ts) |
| Ambiguous (e), operator decision | Infra-LLM Anthropic-only cluster (14 sites, one product question) + 10 individual sites |
