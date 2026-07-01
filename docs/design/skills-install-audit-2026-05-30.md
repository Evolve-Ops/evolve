# Skills install — end-of-roadmap audit (2026-05-30)

After shipping 4 paste-token rewires (Obsidian #1817 / Dropbox #1819 /
Notion #1831 / Linear #1834) plus the planned Runway (#1833) and
GitHub-MCP (#1832) follow-ons, PodAdmin asked for an explicit audit pass:

> "Then when we get to the end we are going to go back and audit what
> we have done and make sure we have selected the right approach per
> skill."

This is that pass.

---

## Method

For each shipped skill, three questions:

1. **Pattern correctness** — was MCP vs bundled-plugin vs OAuth-plugin the right call?
2. **Bundled-extension coverage** — does OC ship a bundled extension that overlaps or supersedes the chosen pattern? (Lesson from Runway: don't assume MCP when OC has it bundled.)
3. **Credential storage shape** — verbatim vs encoded; keystore vs auth-profiles.json vs openclaw.json; per-bot vs pod-wide. Right for this skill?

Cross-check against the **full list of 95 OC bundled extensions** (audited
on the mini at `/opt/homebrew/lib/node_modules/openclaw/dist/extensions/`,
2026-05-30):

```
active-memory admin-http-rpc alibaba anthropic arcee azure-speech bonjour
browser byteplus canvas cerebras chutes clickclack cloudflare-ai-gateway
comfy copilot-proxy deepgram deepinfra deepseek device-pair
document-extract duckduckgo elevenlabs exa fal file-transfer firecrawl
fireworks github-copilot google gradium groq huggingface
image-generation-core imessage inworld irc kilocode kimi-coding litellm
llm-task lmstudio mattermost media-understanding-core memory-core
memory-wiki microsoft microsoft-foundry migrate-claude migrate-hermes
minimax mistral moonshot nvidia oc-path ollama open-prose openai
opencode opencode-go openrouter perplexity phone-control policy qianfan
qwen runway searxng senseaudio sglang signal skill-workshop speech-core
stepfun synthetic talk-voice tavily telegram tencent thread-ownership
together tokenjuice tts-local-cli venice vercel-ai-gateway
video-generation-core vllm volcengine voyage vydra web-readability
webhooks xai xiaomi zai
```

---

## Per-skill verdicts

### Obsidian (PR #1817) — ✅ MCP install
- **Pattern**: MCP via `@modelcontextprotocol/server-filesystem` + extra_args=[vault_path] + OS-ACL mode toggle
- **Bundled check**: no `obsidian` / `vault` / filesystem-MCP equivalent in the 95
- **Storage**: mode marker at `~/.openclaw/skills/obsidian_vault.json` (not a credential — just `{vault_path, mode}`); ACL grants are filesystem-level
- **Verdict**: ✅ correct. The mode-marker shape generalizes (Dropbox reused it). The filesystem MCP server is first-party Anthropic, well-vetted.

### Dropbox (PR #1819) — ✅ MCP install
- **Pattern**: MCP via `@modelcontextprotocol/server-filesystem` + extra_args=[dropbox_path] + OS-ACL mode toggle
- **Bundled check**: no `dropbox` in the 95; the desktop client itself sits at `~/Dropbox` so filesystem MCP is the right abstraction
- **Storage**: same mode marker shape as Obsidian
- **Verdict**: ✅ correct. Auto-detect via `~/.dropbox/info.json` is a nice touch — no analog needed for Obsidian since users pick the vault path.
- **Could-have-been**: a Dropbox-API MCP server (would handle cloud-only files, version history, sharing). Filesystem path was the right v1 — covers the dominant "files in my Dropbox" use case without API credentials. Revisit if/when a user actually needs cloud-only-file or sharing operations.

### Notion (PR #1831) — ✅ MCP install
- **Pattern**: MCP via `@notionhq/notion-mcp-server` + env_bindings OPENAPI_MCP_HEADERS (JSON-encoded headers blob)
- **Bundled check**: no `notion` in the 95
- **Storage**: per-bot keystore slot `notion-<bot>` storing the JSON headers blob
- **Verdict**: ✅ correct. First-party MCP server — best vetting profile of all the rewires. JSON-encoded headers blob is awkward but it's what the upstream server wants; `build_headers_json` hides this from operators.
- **Post-install callout** about per-page sharing is load-bearing — Notion's permission model surprises everyone the first time.

### GitHub-MCP (PR #1832 — pending merge) — ⚠️ caveat below
- **Pattern**: MCP via `@modelcontextprotocol/server-github` + env_bindings GITHUB_PERSONAL_ACCESS_TOKEN (verbatim PAT)
- **Bundled check**: `github-copilot` IS bundled in OC, BUT it's an **LLM provider** (serves GitHub Copilot as inference, uses GITHUB_TOKEN env var for that purpose) — NOT a repo/issue/PR API surface. **Pattern is still correct.**
- **Storage**: per-bot keystore slot, verbatim PAT (mirrors what Linear ended up doing)
- **Verdict**: ✅ correct given the deprecated-package caveat already in the PR description
- **Open item from prior conversations**: per CLAUDE.md memory, the `@modelcontextprotocol/server-github` upstream is deprecated. The PR shipped it anyway because no first-party replacement exists yet. Worth filing an upstream issue or watching for GitHub's official MCP server.

### Runway (PR #1833 — pending merge) — ✅ bundled-plugin install
- **Pattern**: bundled-plugin via `@openclaw/runway-provider` (in OC's `dist/extensions/runway/`)
- **Bundled check**: ✅ `runway` IS in the 95 — confirms the bundled-plugin path
- **Storage**: `auth-profiles.json` under `profiles["runway:default"]` (same shape as Google OAuth, xAI, Anthropic). Plugin config in `openclaw.json::agents.defaults.videoGenerationModel.primary`.
- **Verdict**: ✅ correct. **This is the audit's biggest near-miss** — the original roadmap had Runway as "blocked, no MCP server exists." PodAdmin's correction (TeamBotA had Runway working pre-Evolve via OC's bundled provider) saved us from shipping the wrong install shape. The general lesson — **audit OC's `dist/extensions/` BEFORE assuming MCP is needed** — is now baked into the memory file (`project_obsidian_mcp_install_pattern.md`).

### Linear (PR #1834 — this PR) — ✅ MCP install
- **Pattern**: MCP via `linear-mcp` (dvcrn, community) + env_bindings LINEAR_API_KEY (verbatim PAT)
- **Bundled check**: no `linear` in the 95 — confirmed via audit before implementing
- **Storage**: per-bot keystore slot `linear-<bot>` storing verbatim PAT (NOT JSON-encoded like Notion — `linear-mcp` reads a plain env var)
- **Verdict**: ✅ correct given the candidate-vs-approved vetting status. Community package risk is real but mitigated by version-pin recommendation.
- **Post-install callout** about identity (bot acts as API key holder) is the equivalent surprise-mitigator for Linear's workspace-membership permission model.

---

## Cross-cutting findings

### F1 — Bundled-extension audit is now standing procedure
The Runway near-miss validated this. Every future paste-token rewire (HA
remaining, plus any new skill candidate) must `ls
/opt/homebrew/lib/node_modules/openclaw/dist/extensions/` first. The
audit takes 30 seconds and prevents shipping the wrong install shape.

### F2 — Four near-identical `_create_<skill>_mcp_proposal` closures
Each MCP-backed wrapper route has a copy of this 8-line closure (one for
obsidian, dropbox, notion, linear). All four are functionally identical
except for the originating skill module's name in the docstring. **Refactor
candidate** for follow-up PR: extract `_create_mcp_skill_proposal(action_kind,
payload, bot_id, summary)` as a module-level helper inside the same closure
scope where `_operator_create_apply` already lives. ~30 lines saved + one
place to change the RiskTag shape if it evolves.

### F3 — `resolve_status_mcp` shape converged
Three skills (Notion, GitHub-MCP, Linear) now share the same MCP-status-
resolver shape: `(read_oc_config, read_keystore_slot)` callables + the same
`valid/revoked/missing/unknown` state vocabulary + the same keystore-
unreachable fallback. Could extract a `make_mcp_status_resolver(server_id,
keystore_slot_prefix)` factory, but the per-skill `InstallStatus` dataclasses
have different metadata fields (workspace_name, viewer_name, integration_name,
scopes) that resist a clean generic. **Verdict: leave as-is.** The
near-duplication is shallow; the deeper unification cost outweighs the
savings.

### F4 — Other bundled extensions worth re-examining
The 95-extension audit turned up several entries that overlap with current
skills or could enable new ones. Worth tracking separately as follow-up
investigations:

| Bundled extension | Current state | Worth investigating? |
|---|---|---|
| `imessage` | We have iMessage skill via TCC (`imessage_install.py`) | YES — does the bundled extension expose a higher-leverage tool surface? Could we standardize on it? |
| `telegram` | We have Telegram skill via `telegram_install.py` (writes channels.telegram + kickstart) | LIKELY OK — current install probably already uses the bundled extension. Verify the wiring still matches. |
| `signal` | Signal-cli substrate vetted (memory file project_signal_cli_vetting_2026_05_14) but no skill shipped | YES — bundled extension may obviate the signal-cli wrapper entirely |
| `webhooks` | Not currently used | YES — could enable HA-style notification flows without a custom adapter |
| `browser` | Not currently used | Maybe — overlaps with Brave Search but for richer browsing |
| `microsoft` / `microsoft-foundry` | Not currently used | Maybe — could enable Outlook/Office MCP-equivalent flows; check user demand first |
| `phone-control` | Not currently used | Maybe — overlaps with iMessage in the "call me / text me" surface area |
| `policy` | Used via Slack policy layer | OK |
| `skill-workshop` | Not currently used | Could be relevant to the conversational-bot-creation work (project_conversational_bot_creation_wizard) |

**Recommended next step on F4**: dedicated follow-up sprint where each
candidate gets its own audit-and-decide pass. Don't bundle into the
paste-token-rewire roadmap.

### F5 — Vetting status discipline
Three rewires shipped with `vetting_status="approved"` (Obsidian/Dropbox
via first-party `@modelcontextprotocol/server-filesystem`, Notion via
first-party `@notionhq/notion-mcp-server`). One shipped with `"candidate"`
(Linear via community `linear-mcp`). GitHub-MCP shipped with the
existing catalog entry's status (which I haven't double-checked — note
to verify).

**Standing rule** for future MCP additions: `approved` requires
first-party OR ~2-week real-bot runtime hours; otherwise `candidate`.
The vetting_notes field should always carry the trust contract (who
reviewed, when, version-pin recommendation, what would change the
status). Linear's catalog entry is the reference shape.

### F6 — One real architectural gap surfaced: tools-allowlist
Home Assistant's "read mode vs control mode" question forces the issue:
**OC doesn't have a first-class tool-denylist for MCP servers today**
(confirmed during Obsidian work). The OS-ACL toggle works for filesystem
skills but won't generalize to API skills where mode is enforced server-
side. HA implementation is blocked on this — either OC ships a
tools-allowlist field, or we build a wrapper proxy, or we accept "all
tools" mode as the only HA option (unacceptable given physical-device
control).

**Recommended next step**: file upstream OC issue + add to the
substrate-adoption-priority list. Until resolved, defer HA.

---

## Follow-up tasks (prioritized)

1. **F2 refactor**: extract `_create_mcp_skill_proposal` shared helper. S, high payoff (cleanup before the next MCP skill compounds the duplication).
2. **F4 investigations**: bundled-extension audit for imessage / telegram / signal / webhooks. Could free up significant integration work.
3. **F6 upstream issue**: OC tools-allowlist for MCP servers (blocks HA).
4. **GitHub-MCP vetting refresh**: verify the catalog entry's vetting_status matches the deprecated-package reality.
5. **Linear vetting bump**: schedule `candidate → approved` review for 2 weeks out (~2026-06-13).

---

## What worked

- The "5-piece pattern" (extra_args + wrapper route + mode marker + status resolver + access panel) generalized cleanly across all 4 MCP installs.
- Per-bot keystore slot pattern (mirrors Telegram) was the right call — each bot can connect to a different workspace.
- Validate-before-keystore-write rule prevented bad data from landing in 100% of route tests.
- Rollback on InstallMcpServer failure kept the system consistent across all 4 routes.
- The bundled-plugin pattern (Runway) coexists cleanly with the MCP pattern — distinct enough to pick the right one, similar enough that the status resolver / install plan / access panel shapes carry over.

## What I would do differently

- **Front-load the OC bundled-extension audit** as step 1 of every paste-token rewire (rather than discovering mid-implementation, as happened with Runway).
- **Extract the shared `_create_mcp_skill_proposal` helper at the 2nd duplication** (after Dropbox) rather than waiting until the 4th. The "two-then-extract" rule would have saved ~40 lines and four review-attention units.
- **Catalog vetting_notes earlier** — the Linear entry's notes are the right shape; Obsidian/Dropbox/Notion's older entries are sparser and worth backfilling.
