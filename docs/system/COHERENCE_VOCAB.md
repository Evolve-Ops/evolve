# COHERENCE_VOCAB.md — App Coherence Vocabulary For Bot-Side Repair

Stable vocabulary the bot can rely on when its primary user asks "what
apps are broken?" or "help me fix the journal app" in a Telegram /
Slack / Discord chat. The bot doesn't need to look up assertion IDs or
guess severity meanings — it sees this whole vocab in the session
prefix at session_start.

Injected into every session's system prompt by
`packages/analyzer/session_surface.py::_load_coherence_vocab()`, same
channel as `RUNTIME_NOTES.md`. The marker block below (delimited by
`evolve-coherence-vocab:begin` / `:end` HTML comments) is the text the
bot actually sees; everything outside the marker block is
operator-facing context.

Source of truth:
- Pass A: `packages/admin/evolve_admin/applications/coherence_pass_a.py`
  (`ASSERTION_IDS` constant + each `check_c_a*` function).
- Pass C1: `packages/admin/evolve_admin/applications/coherence_pass_c1.py`
  (`CHECK_C1_*` constants + each `_check_*` function).

Treat this file as a stable interface. When new assertions or check IDs
ship, update both the source file and this doc in the same PR. Don't
programmatically generate this — it's a curated reference, not a
registry.

Maintained by: Evolve. **Review when adding a new Pass A assertion or
Pass C1 check.**

<!-- evolve-coherence-vocab:begin -->
[COHERENCE VOCAB — vocabulary used by your app findings]

Findings on your apps live at
~/.openclaw/workspace/manifests/<app>.json under .coherence.findings[].
Each finding carries an `id` (Pass A) or `check_id` (Pass C1), a
`severity`, an `assertion`, a one-line `description`, and `evidence[]`.

Pass A — manifest-internal graph walk (cheap, runs every Tier 2 tick):
- C-A1 recurring_behavior_without_trigger (critical) — description
  claims daily/weekly/recurring behavior but scheduled_actions[],
  crons[], and oc_heartbeat_instruction are all empty. Soft variant:
  recurring_behavior_only_suspect_actions (minor) — entries exist but
  all tagged quality:"suspect"; promote one or prune.
- C-A2 action_input_not_resolved (major) — scheduled_action input path
  is not in files[] and doesn't match any volatile_paths glob. Fix: add
  the file, declare a volatile_paths entry, or mark the input external.
- C-A3 action_output_no_producer (major) — scheduled_action output has
  no plausible producing mechanism (no code file, no messaging
  integration, no volatile_paths match). Heuristic — sometimes a false
  positive on terse output names; mark_resolved if intentional.
- C-A4 messaging_output_no_integration (critical) — action declares a
  messaging output but requirements.integrations[] has no messaging-
  capable entry (slack/telegram/discord/imessage/whatsapp/signal/
  email/gmail/twilio/sms). Fix: add the integration, or change the
  output kind.
- C-A5 cron_script_not_in_files / cron_script_not_code_layer (major) —
  crons[*].script path is not in files[], or is present but not layer
  "code". Fix: declare the script in files[] with layer:"code".
- C-A6 orphan_code_file (minor) — code-layer file isn't referenced by
  any scheduled_action, cron, test_command, interface_contract, or
  other code file. Could be dead code, or owned_by:"admin"/"external"
  if scheduled externally.
- C-A7 integration_not_referenced (minor) — declared integration id
  appears nowhere in scheduled_actions / crons / files / test_command /
  interface_contract. Drop the integration or wire it in.
- C-A8 cli_command_not_resolved (major) — interface_contract.cli
  command references a script path not in files[]. Fix the path or
  declare the file.

Pass C1 — code-shape static analysis (weekly, AST + import graph):
- C1-1 integration shape (warning) — output integration declared but
  the implementing script doesn't import or invoke anything matching
  that integration (e.g. telegram-output without telegram/telethon
  import).
- C1-2 input shape (warning) — file-read inputs declared but script
  doesn't use read_text / open / equivalent.
- C1-3 LLM shape (warning) — action summary uses "summariz" /
  "analyz" / "drafts" / "generate" but the script doesn't invoke an
  LLM (no anthropic/openai/openclaw import or `openclaw agent`
  subprocess).
- C1-4 cron-script parses (warning) — crons[*].script doesn't parse
  cleanly (ast.parse for Python, `bash -n` for shell).
- C1-M missing file (major) — file the action implements doesn't exist
  at all. Not a shape problem, surfaced for visibility.

Severity scale (across both passes):
- critical — blocks the app's stated job. Repair before next user
  interaction if possible.
- major — feature broken or contract violated; fix soon.
- minor — heuristic miss / dead-code / unreferenced integration; ack
  or schedule.
- info / warning — informational; don't escalate. Pass C1 emits
  "warning"; treat as minor for repair-conversation priority.

Reconciliation status (.reconciliation.status on each manifest):
- ok — added_files/removed_files/drifted_fields are all empty.
- drifted — at least one of those lists is non-empty; operator
  hasn't approved/promoted yet. `evo app-changes <app>` walks the
  detail.
- orphan — every infrastructure file declared by the manifest is
  missing on disk. App may have been retired; reinstall or archive.

Provenance sources (.provenance.field_origins[<field>].source):
- observational — scanner-discovered, not authored. Drift is normal;
  approve to acknowledge.
- forge_built — produced by forge during install. Treat as
  bot_authored for drift purposes.
- user_authored — operator wrote this field. Drift here is meaningful.
- bot_authored — bot proposed it via repair / evo flow. Drift here is
  meaningful.
- confirmed — operator promoted an observational field to authored.
  Drift here is meaningful.

For the assertion-vocabulary primary reference: source is
`packages/admin/evolve_admin/applications/coherence_pass_a.py`
(Pass A) and `coherence_pass_c1.py` (Pass C1). This block summarizes
those — don't invent new ids or assertions.
<!-- evolve-coherence-vocab:end -->

---

## Why this vocab needs to be stable

The session-prefix block is one of the only ways the bot has to know
what `C-A1` means when its user says "the journal app has a C-A1
finding — what's that?" or when the bot itself wants to refer to a
finding by id without paraphrasing.

If we let assertion IDs drift without updating this file, the bot
either:
- Confidently makes up a meaning (the
  `evo confabulation failure mode`),
  or
- Punts and tells the user "I don't know what that finding means."

Both are bad. Treating the vocab as a curated interface — pinned to
source-of-truth modules, reviewed when those modules change — keeps
the bot's repair conversations grounded.

## Out of scope

- Pass C2 (LLM monthly via Tier 3) and Pass C3 (LLM capability check)
  emit verdicts via different fields (`coherence.last_capability_check`)
  and don't use the assertion-id vocabulary. They're not in this doc.
- Finding signatures (`signature: <16-hex>`) are content-addressed and
  stable; the bot uses them when proposing `mark_resolved`.
