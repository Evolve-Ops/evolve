# Primary Bot as Knowledgeable Evolve Interface — Spec (2026-05-14)

Status: **draft** — design-sync target. Not implemented.

**What this is.** The architecture for turning the primary bot into a grounded conversational interface to Evolve — parallel to the HTML admin UI — without LLM hallucination about a private codebase. Covers docs access, live-state retrieval tools, free-form bug / feature-request intake, system-prompt scaffolding, and migration for existing pods.

**Terminology** (locked elsewhere; restated for clarity).
- **Admin** — the human pod sysadmin.
- **Primary bot** — the bot admin talks to; selectable in the wizard; one per pod. Role flag = `"primary"` on `network.bots.<id>.role`. The default dedicated primary-bot identity is **`evo`** (renamed from `evolve` as of 2026-05-14); a member bot can be designated primary instead, in which case its display name is left untouched.
- **Member bot** — every other bot in the pod.

**Related specs.**
- `docs/spec-evo-wizard-2026-05-05.md` — evo dispatch surface; the *action* layer this spec complements.
- `docs/spec-alerts-signal-store-2026-05-07.md` — the signal store, which several read-tools surface.
- `docs/spec-mcp-administration-2026-05-10.md` §"Naming note" — distinguishes the existing **Evolve MCP Bridge** (Evolve-as-MCP-server for Claude Desktop) from the *bots'* MCP clients. This spec adds new tool surface on the bots' side; the bridge is unaffected.
- `docs/operator-message-style.md` — Team-Bot-A-style message shape these answers must follow.

---

## 1. The problem

The primary bot today is two things: an alerts/proposals relay and an `evo`-dispatch host. Both are structured surfaces. The third surface — *open-ended conversation about how Evolve works and what the pod is doing* — is unmet:

- **Docs access is zero.** None of `docs/`, `docs/help/`, or `docs/spec-*.md` is in any system prompt or tool path. When admin asks "how do I add a bot?" the bot has nothing to retrieve from and will confabulate.
- **Live-state retrieval is partial.** Structured queries map onto existing `evo` subcommands (`cost`, `usage`, `alerts`, `health`, `integrations`, `apps`, `summary`, …) but free-text questions ("why is team-bot-a's audit failing?", "show me recent signals") have no read-tool surface from inside the bot's session.
- **No bug-report intake.** Admin can't say "filing a bug: when I do X, Y happens" and have it captured. The current path is "open a GitHub issue manually," which is friction-heavy and loses session context (recent turns, current bot, pod state).
- **No anti-hallucination scaffolding.** The bot is not told *not to guess* about Evolve internals.

The HTML admin UI is precise but discovery-poor. The CLI is precise but jargon-heavy (fails the Plex test). A grounded primary-bot conversational surface is the missing third interface — see `project_evolve_elevator_pitch.md`.

---

## 2. Design principles

1. **Retrieval over injection.** Help docs are not pasted into every turn. They're surfaced via a tool the LLM calls when it needs them. Per-turn token cost stays bounded; `feedback_rsi_low_cost_preference.md` applies.
2. **Tools, not paraphrase.** When the question maps onto a canned `evo` command or a structured pod read, the bot calls a tool and renders the structured result Team-Bot-A-style. It does *not* synthesize numbers from prose.
3. **Refuse-or-retrieve.** The system prompt instructs the bot to either retrieve (via the new tools) or say "I don't know" — never to guess from training-data fragments about Evolve.
4. **No new daemons.** Tools are in-process or thin HTTP calls against the existing admin server. No new launchctl jobs.
5. **Per-bot inference.** All LLM calls remain inside the primary bot using its own credentials (`feedback_per_bot_inference.md`). Evolve provides *data* via tools; it does not run inference.
6. **Plex test.** Tool names, error messages, and bot replies use plain verbs. No "RSI," "proposal pipeline," or "applier dispatch" leaks into user-facing strings — those live in spec/admin-UI surfaces.

---

## 3. Architecture overview

Three surfaces compose. The first two already exist; the third is new.

```
                          ┌──────────────────────────────┐
                          │  primary-bot OC session      │
                          │  (per-bot LLM call)          │
                          └──────────────┬───────────────┘
                                         │
                ┌────────────────────────┼─────────────────────────┐
                ▼                        ▼                         ▼
   ┌──────────────────────┐  ┌──────────────────────┐  ┌──────────────────────┐
   │ existing: evo        │  │ NEW: pod-state read  │  │ NEW: help retrieval  │
   │   structured action  │  │   tools (HTTP→admin) │  │   (HTTP→admin)       │
   │   (cost/alerts/apps/ │  │ pod_status,          │  │ evolve_help_search,  │
   │   audit/install/…)   │  │ list_signals,        │  │ evolve_help_read     │
   │                      │  │ list_proposals,      │  │                      │
   │   `evo <sub>` path   │  │ list_audits,         │  │                      │
   │                      │  │ recent_watchdog,     │  │                      │
   │                      │  │ spend_rollup,        │  │                      │
   │                      │  │ describe_bot,        │  │                      │
   │                      │  │ recent_turns,        │  │                      │
   │                      │  │ submit_intake (W)    │  │                      │
   └──────────────────────┘  └──────────────────────┘  └──────────────────────┘
                                         │                         │
                                         └────────────┬────────────┘
                                                      ▼
                                    ┌──────────────────────────────────┐
                                    │  admin server (evolve user)      │
                                    │  reads {shared_dir}/* directly;  │
                                    │  reads docs/help/*.md from       │
                                    │  /Users/Shared/evolve-repo       │
                                    └──────────────────────────────────┘
```

**Why two new surfaces, not one?** Help retrieval and pod-state reads have different cache/staleness/cost profiles and different vetting requirements (help docs ship in the repo and are stable across deploys; pod state is live). Keeping them separate lets the help index ship as a static asset while the pod-state tools stream from `{shared_dir}`.

**Why HTTP→admin and not direct filesystem reads from the plugin?** The TypeScript plugin runs in the bot user's gateway process; the data lives where the `evolve` user owns it. Going through the admin server (which already has the ACLs and is reachable on Tailscale via `gateway.trustedProxies`) keeps the auth surface clean and reuses every existing endpoint pattern.

---

## 4. Help docs surface

### 4.1 Scope — what counts as a "help doc"

| Path | In-scope for primary-bot retrieval? | Reason |
|---|---|---|
| `docs/help/*.md` (20 files, ~2200 lines) | **Yes** — primary corpus | Already user-pitched; phrased for the admin audience. |
| `docs/operator-runbook.md` | Yes | Operationally load-bearing. |
| `docs/getting-started.md`, `docs/overview.md`, `docs/configuration.md` | Yes | Onboarding answers admin actually asks. |
| `docs/applications.md`, `docs/applications-vs-skills.md`, `docs/manifest-spec.md` | Yes | App framework is a primary differentiator. |
| `docs/spec-*.md` | **No** (default) — designer-facing | Detailed, internally inconsistent during drafting, prone to misleading "what we plan to do" framing. |
| `docs/research/`, `docs/design/` | **No** | Stale by design. |
| `docs/postmortem-*.md` | No | Operationally inert. |
| `CLAUDE.md` (root) | No | Dev guidance, not admin guidance. |

The set is built once at deploy time (`evolve-admin help-index build`) and shipped as `{shared_dir}/help_index.json`. The index is a flat list of `{doc_id, title, summary, path, size, sha}`. Roughly 25 docs at v1; ~150 LOC of index JSON.

### 4.2 System-prompt sidebar — the "table of contents" block

At session start, `session_surface.build_session_prefix` on a **primary bot** appends a short sidebar (~30 lines hard cap):

```
## Evolve help (for admin questions about this pod)

These topics have grounded help docs. Use the `evolve_help_search` tool
before answering admin questions about Evolve internals; if no doc
matches, say "I don't have a doc on that — try the admin UI or `evo help`."

- overview, capabilities, modules        — what Evolve is and what it does
- cost, cost-measures, ai-optimization   — spend, budgets, model tiers
- security, monitoring, meta-health      — alerts, audits, host health
- self-improvement, proposals, generators — the Better Engine loop
- continuity, profile, profile-inferrer  — user profile + memory
- forge, gallery                          — app authoring
- integrations-keys                       — keystore + credential rotation
- maintenance                             — backups, retention, rotation

For pod-state queries (current spend, firing alerts, audit results,
…) prefer the matching `evo <sub>` and report what the structured
result says rather than synthesizing.
```

The sidebar is generated from the index, not hand-written, so it stays in sync as help docs are added. Total tokens: <400. Acceptable per-turn cost; not paid for member bots.

### 4.3 Tools

**`evolve_help_search(query: str, k: int = 3) -> list[Hit]`**
- BM25 or trigram over `title + summary + body` from the help index. No vector store, no embeddings, no LLM in the loop. ~150 LOC of pure Python in `evolve_admin/help_search.py`.
- Returns `[{doc_id, title, snippet (≤500 chars), score, path}]`. The snippet is a windowed extract around the highest-scoring match.
- Cost: a single tool call returns ≤3KB; the LLM either has enough or follows up with `evolve_help_read`.

**`evolve_help_read(doc_id: str) -> {title, body}`**
- Reads the full doc from the help index. Body is the raw markdown.
- Cap at 50KB per call. Largest current help doc is ~12KB.

**Routing.** Both tools are exposed only to the primary bot. Implementation: a new endpoint pair on the admin server (`POST /api/evo/help/search`, `POST /api/evo/help/read`) that the plugin proxies as OC tools registered only when `network.bots.<id>.role == "primary"`. Tool registration lives in the existing `evolve_plugin` TS plugin (mirror of how `before_model_resolve` is wired today).

### 4.4 Why not RAG / embeddings

- 25 docs, ~50KB total. BM25 is enough.
- Embedding cost rollup is still blocked on upstream OpenClaw (see `project_embedding_provider_config.md`). Don't add a new dependent surface before that lands.
- Vector indices add operational mass (storage, regeneration on doc change, drift). Trigram/BM25 regenerates in <100ms at deploy.

---

## 5. Live-state read tools

These tools answer "what's the pod doing right now." They're additive to existing `evo` subcommands — `evo` is the *action* / structured-rendering surface; these tools are for *free-text Q&A* where the LLM needs to inspect state before answering.

### 5.1 Tool set (v1)

| Tool | Returns | Backed by |
|---|---|---|
| `pod_status()` | bots[], primary_id, health summary, last-audit timestamps | `network.json` + signal store sweep |
| `list_signals(state="firing", producer=None, bot=None, limit=20)` | recent Signal envelopes | `{shared_dir}/signals/firing/` |
| `list_proposals(state="pending", limit=20)` | Proposal summaries (id, title, status, motivating_signals) | `arbiter.store.iter_proposals` |
| `list_audits(bot=None, limit=5)` | recent audit results | `{shared_dir}/audits/<bot>/` |
| `recent_watchdog(hours=24, limit=20)` | WatchdogEvent log tail | `{shared_dir}/watchdog/<date>.jsonl` |
| `spend_rollup(window="7d", bot=None)` | per-bot $ / tokens / call counts | analyzer cost rollup (same source as `evo cost`) |
| `describe_bot(bot_id)` | role, integrations, apps, recent audit, recent signals | composed from above |
| `recent_turns(turns=10)` | last N admin↔primary turns in this channel, redacted per `security_warden_capture_policy` | session transcript store |
| `submit_intake(kind, body, attach_recent_turns=True)` | intake id; **write tool** | see §6 |

**Design notes.**
- All read tools are **idempotent + cheap**. No locks, no fan-out. Each is a single read against existing on-disk stores.
- Returns are **stable JSON envelopes** that the LLM renders into Team-Bot-A-style messages. Field names match what `evo` handlers already use (`firing`, `motivating_signals`, `producer`, …), so the bot doesn't have to learn two vocabularies.
- Hard caps on every list tool (≤20 items default, ≤100 max) so the LLM can't ask for the universe.
- `recent_turns` returns the admin's *last N user turns + bot's last N replies* from this channel. Used by `submit_intake` to attach context to bug reports without admin having to re-narrate. Subject to `security_warden_capture_policy` — 200 turn / 48 h ceiling, raw at capture, user opt-out applies.

### 5.2 Relationship to the existing Evolve MCP Bridge

The `evolve_admin/mcp_bridge/` package already implements ~16 read/write tools (`list_bots`, `get_pod_status`, `get_proposals`, `get_evolve_metrics`, etc.) for **Claude Desktop**. This spec deliberately does *not* point the primary bot at that bridge as an MCP client.

**Why not.** The bridge is a Tailscale-exposed full-pod MCP server with write tools (`add_to_memory`, `create_handoff`, `write_workspace_file`) intended for an external admin client. Giving the primary bot's LLM the same surface would:
- Expose write tools the bot doesn't need (handoffs, memory writes belong to admin's Claude Desktop, not to the chat LLM that the admin's *user* is talking to);
- Tie the chat path to an external service the bridge owns, with auth/session lifecycle that doesn't match per-turn tool calls;
- Cross the "MCP servers the bots connect to" trust boundary that `spec-mcp-administration-2026-05-10.md` is being built to lock down.

**What to share.** The *handler implementations* (e.g. `tool_get_pod_status`, `tool_get_proposals`) are reusable Python. Refactor them into `evolve_admin/pod_state/` and have both the MCP bridge and the new `/api/primary/state/*` endpoints call the same library functions. One implementation, two transports.

### 5.3 Auth boundary

- The admin server runs as `evolve` and has ACL read across `{shared_dir}` and bots' `.openclaw/`.
- The plugin (running as the bot user, e.g. `team-bot-a`) calls back into the admin server over loopback (or Tailscale on a remote-bot deploy) using the existing `gateway.trustedProxies` arrangement.
- New endpoints under `/api/primary/state/*` are **gated to the primary bot's bot-token**. A member bot that somehow gets the endpoint URL still can't hit it.
- All write tools (`submit_intake`) audit-log via the existing audit log used by the MCP bridge.

---

## 6. Bug / feature-request intake

### 6.1 Trigger surface

Three on-ramps:

1. **Explicit subcommand.** Add to `evo` registry:
   - `evo bug <free text>` — opens an intake of kind `bug`.
   - `evo feature <free text>` — opens an intake of kind `feature`.
   - `evo intake list` (admin-only) — show recent intake entries.
2. **Conversational capture.** When the LLM detects an intake-shaped utterance ("I want to file a bug," "feature request:", "when I do X, Y happens"), it calls `submit_intake` *after* confirming with admin ("Filing this as a bug — confirm?"). Confirmation is required to keep this surface low-noise.
3. **Reaction shortcut (later).** Telegram reply-with-`/bug` to any bot message. Out of scope for v1.

### 6.2 Storage

Intakes write to `{shared_dir}/intake/`:

```
{shared_dir}/intake/
├── open/<id>.json              ← state ∈ open
├── triaged/<id>.json           ← admin has reviewed; awaiting external action
├── filed/<id>.json             ← turned into a GitHub issue or other external ticket
├── closed/<id>.json            ← resolved / dismissed (90-day retention)
└── log/<YYYY-MM-DD>.jsonl      ← append-only state-change log
```

Shape of an intake envelope:

```jsonc
{
  "id": "intake-20260514-a3f9",
  "kind": "bug" | "feature" | "question",
  "state": "open",
  "created_at": "2026-05-14T18:22:10Z",
  "updated_at": "2026-05-14T18:22:10Z",
  "body": "When I run `evo apps`, nothing comes back even though I see them in the UI.",
  "submitter": {
    "user_key": "ext:telegram:11223344",
    "role": "admin",
    "channel": "telegram:11223344"
  },
  "context": {
    "primary_bot": "evo",
    "active_bot": "team-bot-a",                    // if admin was discussing a specific bot
    "git_commit": "eaa14360",               // deploy-checkout HEAD when filed
    "evolve_version": "2026.5.14",
    "recent_turns_excerpt": [               // last 6 turns; redacted per warden policy
      {"role": "user", "text": "evo apps"},
      {"role": "bot",  "text": "(empty)"},
      // ...
    ],
    "active_signals": ["sig-…", "sig-…"]    // ≤5 firing signals at capture time
  },
  "promotion": {                            // populated when state moves to filed
    "github_issue_url": null,               // e.g. https://github.com/evolve-ops/evolve/issues/1234
    "github_issue_number": null,
    "promoted_at": null,
    "promoted_by": null,                    // user_key of admin who clicked promote
    "body_sent": null,                      // exact body posted to GitHub (post-redaction)
    "redactions_applied": []                // ["recent_turns", "user_key", …]
  }
}
```

Storage layer mirrors the signal store: atomic temp-file + rename, owned by the `evolve` user, no `/tmp` staging needed (lives under `{shared_dir}` ACL).

### 6.3 Why capture-then-promote, not direct issue creation

Captures land in `intake/open/` first; promotion to GitHub is an explicit second step (manual or fast-path; both ship in v1). The two-step model is load-bearing for three reasons:

- **PII / privacy.** The recent-turns excerpt is captured raw (per `security_warden_capture_policy`). Going straight to a public repo would leak transcripts, channel ids, keystore-adjacent paths. The promote step redacts before posting and shows admin exactly what will be sent.
- **Quality of bad data.** A free-text "feature request" captured mid-conversation often makes no sense out of context. The `intake/open/` queue lets admin sharpen the description before posting.
- **Reversibility.** A local intake can be edited or dismissed. A GitHub issue can be closed but not unposted.

Admin who's confident can still fast-path: `evo bug --post <text>` or the LLM offering "Want me to file and post this to GitHub?" both capture + promote in a single turn.

### 6.4 Promotion to GitHub (v1)

Promotion is a v1 capability. Three on-ramps, one mechanism.

**On-ramps.**
1. `evo intake promote <id>` from the chat surface.
2. "Promote to GitHub" button on the admin-UI Intake sub-page (per-row).
3. `--post` flag on the capture commands (`evo bug --post <text>`, `evo feature --post <text>`) and the equivalent confirmation prompt when the LLM detects an explicit "file this to GitHub" intent.

**Mechanism.** `POST /api/evo/intake/<id>/promote` on the admin server. The endpoint:
1. Loads the intake envelope.
2. Computes the redacted body (see §6.5 below) and persists it under `promotion.body_sent`.
3. Calls `POST https://api.github.com/repos/<owner>/<repo>/issues` directly via `httpx`, authenticated with a token from the keystore slot `keystore.github_intake.token`. (Direct REST, not `gh` CLI — the admin server runs as `evolve`, which has no `gh` auth context; a keystore slot is the cleaner boundary.)
4. On success: moves the intake to `filed/`, writes the issue URL into `promotion`, and emits an audit-log entry.
5. On failure: leaves the intake in `open/`, surfaces the error to the caller, and emits an audit-log entry. No retries — admin tries again.

**Target repos.** Configured under `network.intake.github` in `network.json`. Two schemas, both parsed by the reader.

**v1 — single target (legacy, still supported):**

```jsonc
"intake": {
  "github": {
    "owner": "evolve-ops",     // default for public launch
    "repo": "evolve",
    "labels": {
      "bug": ["intake", "bug"],
      "feature": ["intake", "enhancement"],
      "question": ["intake", "question"]
    },
    "token_slot": "github_intake"
  }
}
```

**v2 — multi-target (added 2026-05-22 per `docs/spec-issue-inbox-2026-05-22.md`):**

```jsonc
"intake": {
  "github": {
    "default": "evolve",
    "targets": {
      "evolve": {
        "owner": "evolve-ops",
        "repo": "evolve",
        "labels": {"bug": ["intake", "bug"], "feature": ["intake", "enhancement"]},
        "token_slot": "github_intake"
      },
      "openclaw": {
        "owner": "openclaw",
        "repo": "openclaw",
        "labels": {},
        "token_slot": "github_intake_openclaw"
      }
    }
  }
}
```

Reader rules:
- If `targets` is present, the v2 path is taken. `default` names the fallback target for un-suffixed promotes; if `default` is missing or doesn't match a known target, the first declared target wins.
- Otherwise, if `owner` + `repo` are present at the top, the v1 path is taken and exposed to callers as a single target named `default`.
- Otherwise the block is treated as absent.

Multi-target promote: `evo intake promote <id> --to <name>` or, in the web API, `POST /api/evo/intake/<id>/promote {"target": "<name>"}`. Omitting the name uses the configured default.

`owner`/`repo` defaults during the public-launch transition: `evolve-ops/evolve`. During the private-dev phase it can be set to `evolve-ops/evolve` per `project_repo_and_launch.md`. If the block is absent, promotion endpoints return a 412 with a helpful message ("intake.github not configured — run `evolve-admin intake configure`"). An unknown `--to <name>` also returns 412 and lists the configured target names.

Migration path: existing v1 installs keep working unchanged. The first `evolve-admin intake configure --name <X>` call upgrades the file in-place: the existing v1 entry is folded into the new `targets` dict under the name `default`, and the new target is added alongside.

**Issue body shape posted to GitHub.**

```markdown
**Intake** — `intake-20260514-a3f9` (bug, captured 2026-05-14T18:22:10Z)

When I run `evo apps`, nothing comes back even though I see them in the UI.

---

### Context

- Active bot at capture: `team-bot-a`
- Pod commit: `eaa14360` (evolve 2026.5.14)
- Firing signals at capture: 2 (signature-redacted)

_(Recent transcript redacted by default; admin can re-promote with `--include-transcript` to attach.)_
```

Compact, no PII by default. The `--include-transcript` flag (and an admin-UI checkbox) opts in to including the redacted transcript excerpt.

### 6.5 Redaction policy for promotion

Applied by `intake/promote.py`:

| Field | Default | Notes |
|---|---|---|
| `submitter.user_key` | redact | replaced with `"admin"` |
| `submitter.channel` | redact | replaced with `"(redacted)"` |
| `context.recent_turns_excerpt` | redact (drop entirely) | opt in via `--include-transcript`; even then, runs through warden's transcript redactor first |
| `context.active_signals` | redact (count only, no ids) | signal ids may identify components |
| `context.primary_bot` | keep | bot id is in `network.json`, which is repo-internal but admin-known |
| `context.active_bot` | keep | same as above |
| `context.git_commit`, `evolve_version` | keep | public info |
| `body` (free text) | keep verbatim | admin wrote it; their problem to redact |

Redactions applied are recorded in `promotion.redactions_applied` so the admin-UI can show "what got stripped."

### 6.6 Triage path

`evo intake list` (chat) and the admin-UI Intake sub-page (per `project_alerts_page_subscriptions.md`: "Alerts page is home for these operator surfaces") let admin browse open intakes, edit body before promotion, choose include/exclude transcript, and promote. State transitions:

```
open ──(admin reviews)──▶ triaged ──(promote)──▶ filed
  │                          │                     │
  └──(dismiss)───────────────┴─────────────────────┴──▶ closed
```

A future monitor (out of scope) can fire a Signal when `intake/open/` size exceeds a threshold.

---

## 7. System-prompt scaffolding (anti-hallucination)

Append a primary-bot-only block in `session_surface.build_session_prefix`. New parameter `primary_block` joined after `app_posture_block`, before `notifications_block`:

```
## You are this pod's primary bot

Admin (the human pod sysadmin) talks to you for two things:

  1. Operational questions about Evolve — how something works, what state
     the pod is in, why something fired.
  2. Filing bugs and feature requests against Evolve itself.

For (1):
  - For "what is X / how do I do Y" questions: call `evolve_help_search`
    first. If a doc matches, ground your answer in it. If nothing matches
    closely, say "I don't have a doc on that — try `evo help` for commands,
    or the admin UI." Never invent file paths, command flags, or behaviors.
  - For "what's the pod doing / show me X" questions: prefer the matching
    `evo <sub>` command and tell admin to run it, OR call the matching
    read tool (pod_status, list_signals, list_proposals, list_audits,
    spend_rollup, describe_bot) and render the structured result in
    Team-Bot-A style. Do not synthesize numbers from prose.
  - For "why did X happen" questions: combine — fetch the relevant signal /
    audit / proposal via tools, then use evolve_help_search for context.

For (2):
  - If admin says "filing a bug," "feature request," or describes a
    misbehavior, confirm intent ("Want me to file this as a bug?"), then
    call `submit_intake` with kind=bug|feature and the body. Echo the
    intake id back.
  - If admin explicitly says "post this to GitHub" or "file this as an
    issue," confirm once ("Posting to GitHub — confirm? (transcript will
    be stripped)"), then call `submit_intake` with promote=true. Echo
    the GitHub URL back on success.

Style: short header, one fact per line, conversational close-out. Never
label findings as "CRITICAL" / "Security" / "P0" unless they actually
came from a security-warden signal. Plain verbs only — admin is technical
but doesn't speak Evolve internals.

If you don't know, say so. Don't guess.
```

Token budget: ~350. Pairs with the help sidebar (§4.2) for ~750 added system-prompt tokens on the primary bot. Member bots are unaffected.

This block is generated by `session_surface.load_primary_block(bot_id)` (new function), keyed on `network.bots.<bot_id>.role == "primary"`. Easy to A/B by deploying a single primary bot with the block disabled.

---

## 8. Overlap with `evo` dispatch — routing rules

The two surfaces coexist. Decision rule baked into the system prompt and reinforced by the help index:

| Question shape | Route |
|---|---|
| "what's spend this week?" | LLM calls `spend_rollup` tool, renders. **Or** tells admin to run `evo cost`. (Either is acceptable. Don't synthesize.) |
| "show me firing alerts" | LLM calls `list_signals` tool *or* defers to `evo alerts`. |
| "what bots do I have?" | `pod_status` tool. |
| "how do I add a bot?" | `evolve_help_search("add bot")` → render grounded answer from `docs/help/`. |
| "why did team-bot-a fail its last audit?" | `list_audits(bot="team-bot-a")` + `evolve_help_search("audit")` → narrate the finding with grounding. |
| "what's the proposal queue?" | `list_proposals` tool. |
| "filing a bug: X" | Confirm → `submit_intake(kind="bug", ...)`. |
| "what is RSI?" | `evolve_help_search("RSI")` → grounded answer; flag "applied to applications, not skills" per `feedback_rsi_framing_not_self_improving.md`. |

The bot does **not** need to "know" this table. The system-prompt block (§7) and tool descriptions teach it the pattern: retrieve before answering; for structured queries, tools or `evo`.

---

## 9. Persona

No separate persona blob for the primary bot. The pod-conduct injection + the new primary block (§7) carry voice/behavior. Tone target locked elsewhere (`project_evolve_voice_and_first_week.md`): Tailscale/Notion/Plex. Team-Bot-A-style for replies (`feedback_message_style_team-bot-a_like.md`).

The dedicated primary-bot identity is `evo` (locked 2026-05-14, renamed from `evolve`). That's the *display* name; the system prompt remains generic ("you are this pod's primary bot") so member-bots-promoted-to-primary inherit the same scaffolding without acquiring an `evo` persona. Renames don't require code changes.

A member bot promoted to primary via `evolve-admin primary set <bot_id>` does not get its persona rewritten (`project_evolve_bot_role.md` principle: adopting the primary duty must not distort the bot the admin has already configured). It gains the help sidebar, the primary block, and the new tool surface, all keyed on `role == "primary"` — and nothing else.

Note that the human-facing CLI is still `evolve-admin` and the service Unix user is still `evolve` — those are unrelated to the bot identity and are out of scope for this rename.

---

## 10. Upstream OpenClaw — anything to adopt?

Brief check (per `feedback_dont_reimplement_upstream.md` and `reference_openclaw_releases_page.md`):

- OC ships a generic `/help` slash command in the gateway plugin surface. It enumerates available slash commands. Not a conversational interface; not relevant to this spec beyond "don't duplicate the slash-command list under `evo`."
- OC has no built-in "talk to your sysadmin tooling" affordance. Tool registration is the right shape, and OC already supports per-bot tool registration via plugins — which is exactly what `evolve_plugin` does today.
- No upstream feature to wait for. Build the help/state/intake tools as Evolve-native.

This is *not* a candidate for the "adopt upstream, don't reimplement" rule — there is nothing upstream to adopt.

---

## 11. Implementation slot-in points

| Concern | File / Module | Status |
|---|---|---|
| Help index build + search | `evolve_admin/help_search.py` (new) | new |
| Help search/read endpoints | `evolve_admin/server.py` — add `/api/evo/help/{search,read}` | edit |
| Pod-state read tools (library) | `evolve_admin/pod_state/` (new package; refactor handlers out of `mcp_bridge/tools.py`) | refactor + new |
| Pod-state HTTP endpoints | `evolve_admin/server.py` — add `/api/primary/state/*` | edit |
| Intake store | `evolve_admin/intake/store.py` (new) | new |
| Intake endpoints | `evolve_admin/server.py` — add `/api/evo/intake/*` (incl. `<id>/promote`) | edit |
| Intake → GitHub promoter | `evolve_admin/intake/promote.py` (new) | new |
| Intake → GitHub redaction | `evolve_admin/intake/redact.py` (new) | new |
| Keystore slot for GH token | `evolve_admin/keystore.py` — add `github_intake` slot definition | edit |
| Intake config helper | `evolve_admin/cli.py` — add `evolve-admin intake configure` | edit |
| `evo bug` / `evo feature` / `evo intake {list,promote}` subcommands | `evolve_admin/evo/subcommands.py` + `evo/handlers/intake.py` | edit + new |
| Session-prompt sidebar + primary block | `packages/analyzer/session_surface.py` — add `load_help_sidebar_block`, `load_primary_block` | edit |
| Tool registration on primary bot | `packages/plugin/src/evolve_plugin.ts` — register `evolve_help_search`, `evolve_help_read`, pod-state + intake tools when `role==primary` | edit |
| Admin UI: Intake sub-page | `webapp/pages/Alerts/Intake.tsx` (new) | new |

No new daemons. No new launchd jobs. The help index regenerates as part of `deploy.py` (one-line addition); intake retention rolls into the existing daily `signals.retention` cron (extend the script). GitHub promotion is a synchronous HTTP call from the admin server using `httpx` (already a dependency).

---

## 12. Migration for existing pods

Order of operations when this lands:

1. **Help index ships in the repo.** Built at deploy time by `evolve-admin help-index build`. No runtime config change.
2. **Endpoints land on the admin server.** Existing admin server gets new routes; restart is automatic on next `repo-puller` cycle.
3. **Plugin update.** New `evolve_plugin` build registers the new tools — but they're *only registered* when `network.bots.<bot_id>.role == "primary"`. A member bot redeploy is a no-op for this feature.
4. **Primary bot redeploy.** `sudo evolve-admin deploy <primary>` picks up the new system-prompt blocks (help sidebar + primary block) on its next session start. No session-mid breakage; everything is at-session-start scaffolding.
5. **Intake → GitHub one-time config.** Admin runs `evolve-admin intake configure` once to set `network.intake.github.{owner, repo}` and stash a PAT in the `github_intake` keystore slot. Until this is done, capture works fine but promotion endpoints return a 412 with a configure-me message. The default values point at `evolve-ops/evolve` to match the public-launch repo; the wizard offers an override for private-dev pods.
6. **Bot rename `evolve` → `evo` (this work).** Pods with the legacy `evolve` dedicated bot get a one-shot rename: `evolve-admin primary rename evo` updates `network.json` (key + display name), redeploys, and leaves the old `/Users/evolve` Unix user account alone (it's the service user; unrelated to the bot identity). Member-bots-promoted-to-primary are unaffected.
7. **No further `network.json` migration needed.** All other gating is on the existing `role` field.

If a pod has no primary bot (legacy, pre-wizard), this whole feature stays dark — no warning, no log spam. `evo` already handles this case; we reuse the same predicate.

**Rollback.** Set `network.bots.<bot_id>.role = "member"` on the primary; the next session won't get the sidebar/primary block, and the plugin won't register the tools. Endpoints stay live on the admin server but become unreachable from any bot. Already-filed GitHub issues stay filed (irreversible by design); local intake state under `{shared_dir}/intake/` is preserved. No data migration to undo.

---

## 13. Out of scope (deferred)

- Per-bot help docs (e.g. team-bot guides surfaced through the same tool). Likely future work; the index format is generic enough to extend.
- Voice rendering / TTS surface.
- Multi-turn wizard for bug reports ("what bot? what time? attach screenshot?"). v1 is single-shot capture; sharpening happens in admin's review.
- Help retrieval for member-bot users (the team channel asking "how does this bot work?"). Out of scope — different audience, different docs (the bot's *guide*), different system prompt. Mentioned only to avoid scope creep.
- Vector / semantic search. BM25 first. Reconsider only if user testing shows the keyword search misses meaningful queries.
- Reverse sync from GitHub (issue closed → intake updated, comments mirrored). One-way push only in v1.
- A monitor that fires a Signal when `intake/open/` size exceeds a threshold. Easy to add later; not needed at v1 volumes.
- Auto-attaching screenshots / log bundles. Admin can manually attach to the GitHub issue after promotion.

---

## 14. Open questions for the design sync

1. **Help sidebar size.** Is ~30 lines / ~400 tokens the right per-turn overhead, or should the index be even smaller (top-level categories only) and the search tool description carry the discovery weight?
2. **Intake confirmation strictness.** Should `submit_intake` always require confirmation, or auto-submit when the user explicitly says "file a bug:"? Confirmation is safer; auto-submit is friction-lower. And: should the `--post` (capture+promote in one shot) on-ramp also require a second confirmation, or trust the explicit "post to GitHub" intent?
3. **`recent_turns` capture redaction default.** Reuse `security_warden_capture_policy` for *capture* (default-on opt-out, raw at capture, 200/48h). Promotion-time redaction is separately defined in §6.5 and defaults to dropping the transcript. Anything else to add to either layer? E.g., always redact keystore directory paths even from capture?
4. **GitHub PAT scope.** Minimum-viable scopes for the `github_intake` token: `public_repo` if posting to `evolve-ops/evolve` (public), `repo` if posting to a private fork. Should the keystore enforce that the token only has issue-write, not full-repo? GitHub PATs don't expose granular issue-write as a single scope, but fine-grained PATs do — recommend fine-grained as default in `evolve-admin intake configure`.
5. **Tool exposure to non-primary admin contexts.** Should the same tools be available when admin talks to a *member* bot ("hey team-bot-a, what's our spend?") via address routing? Current default: no — only primary gets them. Open to flipping if it tests well.
