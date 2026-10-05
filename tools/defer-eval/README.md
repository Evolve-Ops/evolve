# Defer-tool reliability eval

Two harnesses for measuring how reliably a bot calls the `defer` tool
across a fixed prompt set. Run after every system-prompt change or model
upgrade to track regressions and improvements.

| Harness | What it measures | Setup | Run time | Cost |
|---|---|---|---|---|
| **`api_eval.py`** *(recommended for tightening prompts)* | Pure model behavior given the system prompt + tool description, no session state | `ANTHROPIC_API_KEY` env var, no gateway | 2-3 min | ~$0.20 |
| **`run_eval.py`** *(work-in-progress)* | Production stack: gateway, plugin tools, session context | mini access, real bot session UUID | 10-20 min | ~$1-2 |

**Use `api_eval.py` first.** It gives you a clean per-prompt baseline
without context bleed across prompts. If api_eval shows ≥90% TPR and the
production system seems worse, the gap is in the runtime additions
(pod conduct + tool registration + accumulated session). If api_eval
shows <90% TPR, no gateway-level fix will rescue it — tighten the prompt
or tool description first.

## Why

Continuity Engine v2 puts the bot in charge of detecting deferral. That
works only as well as the model's decision-making. One successful test
during integration doesn't tell us how often the bot gets it right
across varied phrasings.

The eval gives us four numbers per run:

| Metric | What it measures | Target |
|---|---|---|
| **TPR** (true-positive rate) | of prompts that should defer, % that did | ≥ 90% |
| **FPR** (false-positive rate) | of prompts that shouldn't defer, % that did | ≤ 10% |
| **Mode accuracy** | of correct defers, % chose the right mode (message vs action) | ≥ 80% |
| **Time accuracy** | of correct defers with an asserted offset, % within tolerance | ≥ 90% |

## Files

- `prompts.json` — 40 prompts (20 should-defer + 20 shouldn't-defer)
  with expected outcomes. Stable IDs so you can diff results across runs.
- `run_eval.py` — runner that, for each prompt, resets a dedicated
  scratch session, dispatches the prompt to the bot's gateway,
  inspects the queue, scores the result, and removes the test row.
  At end-of-run it deletes the scratch session.

## Methodology — per-prompt session reset

Each prompt runs against a dedicated explicit session keyed by
`agent:main:explicit:defer-eval-<bot>-<YYYYMMDD-HHMMSS>` (auto-generated;
override with `--session-id` if you want a stable label across runs).
Before every prompt the harness calls the gateway RPC `sessions.reset`,
which clears the conversation transcript while keeping the same key.
The next agent run sees a fresh first turn:

- Full system-prompt re-injection (pod conduct, skills snapshot, model
  bootstrap context).
- Full plugin-tool catalog including `defer` (verified via
  `tools.effective` against both telegram-driven and explicit sessions —
  the catalogs are identical).
- No transcript bleed from any previous prompt.

After all prompts run, the harness calls `sessions.delete` to archive
the transcript and remove the session entry from the bot's session
list. Pass `--keep-session` to skip that cleanup if you want to inspect
the per-prompt transcripts post-hoc.

This methodology generalises: any future eval that needs many isolated
turns against a real bot (not just defer) can use the same pattern.

## How to run — `api_eval.py` (recommended)

```bash
export ANTHROPIC_API_KEY=sk-ant-...

# Smoke test 5 prompts first
python3 tools/defer-eval/api_eval.py --limit 5

# Full run with report
python3 tools/defer-eval/api_eval.py \
  --report /tmp/api-eval-$(date +%Y%m%d-%H%M%S).json
```

Each prompt is one Anthropic Messages API call with:
- The pod-conduct summary block (loaded from `docs/system/POD_CONDUCT.md`),
  identical to what `session_surface.py` injects in production.
- Current UTC + local time (so the model can compute relative offsets).
- The `defer` tool definition (parameters + description identical to
  `packages/plugin/src/tools/DeferTool.ts`).
- The user's prompt as a single user message — fresh context, no bleed.

Default model is `claude-sonnet-4-5` (closest publicly-available stand-in for
Admin-Bot's `claude-sonnet-4-6`). Override with `--model claude-haiku-4-5` etc.

### Limitation

`api_eval.py` measures the model's behavior given the system prompt + tool
description in isolation. Production includes additional context: prior
session turn history, other registered tools, runtime-injected channel
context. So api_eval is necessary-but-not-sufficient: it proves the prompt
+ tool-description CAN reliably produce defer calls. Whether the
production stack does the same in practice still needs `run_eval.py`.

## How to run — `run_eval.py` (production stack)

You don't need to find an existing session UUID — the harness creates
its own scratch session.

```bash
ssh mini
cd /Users/Shared/evolve-repo
sudo -u evolve python3 tools/defer-eval/run_eval.py \
  --bot admin-bot \
  --report /tmp/defer-eval-$(date +%Y%m%d-%H%M%S).json
```

Smoke test with the first few prompts (always do this once before a
full run):

```bash
sudo -u evolve python3 tools/defer-eval/run_eval.py --bot admin-bot --limit 3
```

Each prompt takes ~25–30s wall-clock against a freshly-reset session.
A 3-prompt smoke run completes in ~90s; a full 40-prompt run in
~15–20 min.

If the smoke output shows `RESET-FAIL` next to any prompt, the gateway
RPC is unreachable from `evolve` — usually a sign the gateway daemon
isn't running. Run `sudo -H -u <bot> openclaw gateway status` to check.

## Queue-file ownership invariant — load-bearing

The bot's defer plugin (running as the bot user) writes new rows to
`/Users/<bot>/.openclaw/workspace/evolve/defer-queue.jsonl`. The file
**must stay owned by the bot user**. If anything rewrites it as a
different user (e.g., `evolve`), the bot's plugin starts hitting silent
permission errors — the tool reports "scheduled" but no row appears
in the queue, and the eval reports a 0% TPR.

The harness's row-cleanup step uses an in-place truncate-and-rewrite
under the queue's flock (rather than the more idiomatic tempfile +
rename), specifically to preserve ownership. This invariant is
covered by `TestRemoveRowsByIdPreservesOwnership` in the test suite.

If you ever see a 0% TPR run with no obvious cause, check:

```bash
ssh mini ls -l /Users/<bot>/.openclaw/workspace/evolve/defer-queue.jsonl
```

If it's owned by `evolve` instead of the bot user, restore it once with:

```bash
ssh mini sudo /usr/sbin/chown <bot>:staff \
  /Users/<bot>/.openclaw/workspace/evolve/defer-queue.jsonl
```

## What the script does

For each prompt:

1. Calls `openclaw gateway call sessions.reset` for the eval session
   key (clears conversation transcript, preserves the key).
2. Records the bot's current queue (so we can spot any new row).
3. Invokes `sudo -H -u <bot> openclaw agent --session-id <eval-id>
   --message "..." --json` (no `--deliver` — the bot's reply does
   NOT go to the user's Telegram).
4. Waits 2s for the queue file to settle, then reads it.
5. Scores: was the new row's presence/absence as expected? Right mode?
   Right `fires_at` offset?
6. Removes the test row from the queue (in-place rewrite under flock,
   preserving file ownership) so the bot doesn't fire test-message
   defers at the user later.

After the loop, the eval session itself is deleted via
`sessions.delete` (skipped with `--keep-session`).

## Costs and constraints

- Each prompt is ~1 model call. 40 prompts ≈ $1–2 in API spend, ~15–20 min.
- Don't run during peak operator hours — the bot is busy answering you.
- The script must run as a user that can `sudo -H -u <bot>` (root on the
  mini, or `evolve` via the existing sudoers grant for the bot).
- Writes to `/tmp/<report>.json` for the full per-case detail.

## CLI flags

| Flag | Default | Purpose |
|---|---|---|
| `--bot` | (required) | Bot id (e.g. `admin-bot`) |
| `--session-id` | auto: `defer-eval-<bot>-<ts>` | Override the scratch session label |
| `--agent-id` | `main` | Agent id within the bot — almost never needs changing |
| `--prompts` | `tools/defer-eval/prompts.json` | Path to the prompt set |
| `--report` | stdout only | Where to write the JSON report |
| `--limit` | all | Run only the first N prompts (smoke test) |
| `--no-cleanup` | off | Leave eval-produced rows in the queue (will fire to user!) |
| `--no-reset` | off | Skip per-prompt session reset (re-enables context bleed) |
| `--keep-session` | off | Don't delete the scratch session at end-of-eval |
| `--agent-timeout-sec` | 90 | Per-prompt agent invocation timeout |

## Reading a report

stdout shows a summary like:

```
── should-defer (n=20) ──
  TP (called defer):       18  (90.0%)
  FN (missed):              2  (10.0%)

── should-NOT-defer (n=20) ──
  TN (correctly skipped):  19  (95.0%)
  FP (called when not):     1  (5.0%)

── mode + timing (within true-positives) ──
  mode correct:    16/18  (88.9%)
  due_at correct:  15/16  (93.8%)

── failures (n=4) ──
  should-defer-006  [TIME(actual=240m)]  'tell me at 4pm what the weather forecast looks like'
  should-defer-019  [MISSED]  'tell me in 4 minutes the capital of mongolia.'
  shouldnt-defer-005  [FALSE-POS]  'you could check the logs tomorrow if you want, but no rush'
  ...
```

The `--report` JSON has full per-case detail (agent response excerpt,
exact mode/fires_at, error text, per-prompt session-reset status).

## Iterating

When the eval surfaces failures:

1. **Missed (should-defer → didn't defer)**: usually a system-prompt
   gap — the recognition rule isn't firing for that phrasing. Tighten
   the prompt, re-run, see if it improves.
2. **False positive (shouldn't-defer → did)**: usually too-aggressive
   recognition. The same phrasing can read as a request to schedule
   when it's actually advice. Soften the rule.
3. **Wrong mode (called defer with action when message was right)**:
   the model isn't distinguishing "I already know what to say" from
   "I need to look something up later." Adjust the tool description.
4. **Time off**: usually the bot anchors on its own reply time rather
   than the user's prompt time. The system prompt should clarify which.

After each prompt change, re-run the eval and compare the report
JSON across runs (they have stable prompt IDs, so a diff tool can
show which cases improved or regressed).
