# Atlas — Deploy Walkthrough

**Target environment:** the production mini, as the `pod-admin-user` admin user.
**Status:** Atlas v0.1 — first deployment of the `community-research-bot` pattern.

This walkthrough is comprehensive — read top to bottom before running anything.

---

## 0. Pre-requisites

Before you start, have ready:

- [ ] **Mini SSH access** as `pod-admin-user@mini` (see `reference_ssh_mini_host`)
- [ ] **A free port** for atlas's gateway. Check existing ports in `/Users/Shared/evolve/network.json`; pick something not in use (e.g. 19010).
- [ ] **Your numeric Telegram user ID** (resolved in step 4.5 below; declares you as the operator).
- [ ] **Anthropic API key** for atlas's classifier + synthesizer. The cost ceiling is ~$2/month at expected volume.
- [ ] **Brave Search API key.** Pod-wide invariant; should already be configured.
- [ ] **GitHub PAT** (optional). For OpenClaw release feeds. Falls back to RSS if absent.
- [ ] **A Telegram account** to operate as Atlas's BotFather session.
- [ ] **The Telegram group** for OC enthusiasts created and ready (you'll add Atlas later, not now).

---

## 1. Create Atlas in BotFather

In Telegram, talk to **@BotFather**:

1. `/newbot`
2. Display name: `Atlas`
3. Username: pick something unique, e.g. `evolve_atlas_bot` (must end in `_bot`).
4. **Copy the bot token** — you'll paste it into the admin UI later.

**CRITICAL — disable privacy mode now**, so Atlas can see every group message (required for `article-capture`):

5. `/setprivacy` → select your bot → **Disable**
6. Verify: `/mybots` → your bot → "Bot Settings" → confirm "Group Privacy: OFF"

Without this, article-capture will only fire when a member @-mentions @atlas, which defeats the purpose.

**Set the bot's commands list** so members see them in Telegram's autocomplete:

7. `/setcommands` → select your bot → paste:
   ```
   ask - Ask Atlas a focused research question
   optout - Remove a captured URL from Atlas's archive: /optout <url>
   optoutall - Remove every URL you've shared from Atlas's archive
   ```

---

## 2. Register Atlas in the pod

On the mini:

```bash
ssh pod-admin-user@mini
sudo evolve-admin add-bot atlas --port 19010 --role member
sudo evolve-admin deploy atlas
```

`add-bot` creates the macOS `atlas` user, registers atlas in `network.json`, and sets up the workspace ACLs.

`deploy` runs the full 8-step deploy: plugin build, OC plugin install, workspace setup, gateway plist, smoke audit. **If the smoke audit surfaces critical findings, do NOT pass `--allow-audit-criticals` — fix the findings first** (per `feedback_evolve_dev_not_test_pod`).

---

## 3. Install the Telegram skill credential

Open the admin UI → bot `atlas` → **Skills** → **Telegram** → **Install**.

Paste the BotFather token from step 1. Click **Verify and save**.

Evolve calls Telegram's `getMe` to validate. After save, verify that `can_read_all_group_messages` is `true` on the bot's skill status page (this confirms step 1's privacy disable took effect).

---

## 4. Resolve the Telegram group's chat ID

You can't post to a group until you know its numeric chat ID. Easiest way:

1. Add Atlas to the group temporarily (we'll formalize the intro later).
2. Send any message in the group.
3. From any machine:
   ```bash
   curl -s "https://api.telegram.org/bot<BOT_TOKEN>/getUpdates" | python3 -m json.tool | grep -A2 '"chat"'
   ```
4. Find the entry whose `title` matches your group. Note the `id` — it'll be a negative number like `-1001234567890` for supergroups.

Save this chat ID — you'll need it for `ATLAS_CHAT_ID`, the cron args, AND the operator config.

Once you have the chat ID, **remove Atlas from the group** (long-press the bot → Remove). You'll add it back properly at step 12.

---

## 4.5. Resolve your operator Telegram user ID

Atlas's guard layer needs to know two things to operate safely:

- Which Telegram user is the operator (you) — gets admin commands + full access in DM.
- Which group chat_ids are "approved" — Atlas only operates in these groups and verifies DM senders against them.

To find your numeric Telegram user ID, easiest options:

- DM **@userinfobot** or **@getidsbot** on Telegram — they reply with your user_id.
- Or: send Atlas any message (e.g. via the temporary group add from step 4 before you removed it), then run:
  ```bash
  curl -s "https://api.telegram.org/bot<BOT_TOKEN>/getUpdates" | python3 -m json.tool | grep -B2 -A2 '"from"'
  ```
  Find the `"id":` field inside the `"from"` object that matches you.

Save the user ID — it'll be a positive integer like `123456789`.

**Why this matters:** without the operator config, ALL DMs are treated as stranger (silently ignored) and ALL group messages are treated as foreign (silently ignored). Atlas literally won't do anything. So getting this right is not optional.

---

## 5. Copy Atlas scripts to the bot workspace

The four Atlas manifests declare `"workspace_files_source": "docs/atlas-app-manifests/"` —
which means `sudo evolve-admin apply-actions <bot> <app>` syncs the scripts from the deploy
checkout into the bot's workspace automatically. Spec:
`internal/spec-workspace-file-sync-2026-06-07.md`.

**For first-time install** (no manifests on the bot yet), the manifests don't exist on the
bot side yet, so apply-actions has nothing to read. Run the manifest install in step 8
first, then come back here and run apply-actions for each app.

**For re-deploys** (post-PR-merge updates to atlas's scripts), this is the entire procedure:

```bash
ssh pod-admin-user@mini
sudo evolve-admin apply-actions atlas atlas-daily-digest
sudo evolve-admin apply-actions atlas atlas-article-capture
sudo evolve-admin apply-actions atlas atlas-on-demand-research
sudo evolve-admin apply-actions atlas atlas-weekly-recap
```

Each invocation compares the sha256 of every file in `manifest.files[]` against
the workspace copy. Drifted files are re-copied and any stale `__pycache__/` next
to a `.py` file gets cleared so the next invocation runs the new code. Clean
runs are a sub-millisecond no-op.

A workspace_sync stamp lands on each app's manifest after every run:

```json
{
  "workspace_sync": {
    "last_synced_at": "...",
    "source":         "/Users/Shared/evolve-repo/docs/atlas-app-manifests",
    "synced_count":   11,
    "drifted_paths":  ["scripts/atlas_lib/classifier.py", ...],
    "missing_in_source": ["archive/index.json", ...],
    "orphan_paths":   [],
    "errors":         []
  }
}
```

`missing_in_source` lists files declared in `manifest.files[]` that aren't in the
source dir (Atlas's runtime-generated state files: `archive/index.json`, `digest/`
content, `atlas/operator.json`); this is expected and harmless. `orphan_paths`
flags workspace files that used to be declared+sourced but no longer exist on the
source side (v1 logs only; no auto-delete).

For **config templates** (operator-editable) — `operator.json`, `sources.json`,
`research-config.json` — these are only copied at first-install via:

```bash
ssh pod-admin-user@mini
WS=/Users/atlas/.openclaw/workspace
sudo /bin/mkdir -p "$WS/atlas" "$WS/archive" "$WS/digest" "$WS/recap"
sudo /bin/cp /Users/Shared/evolve-repo/docs/atlas-app-manifests/scripts/atlas/sources.json.template "$WS/atlas/sources.json"
sudo /bin/cp /Users/Shared/evolve-repo/docs/atlas-app-manifests/scripts/atlas/research-config.json.template "$WS/atlas/research-config.json"
sudo /bin/cp /Users/Shared/evolve-repo/docs/atlas-app-manifests/scripts/atlas/operator.json.template "$WS/atlas/operator.json"
sudo /usr/sbin/chown -R atlas:staff "$WS/atlas" "$WS/archive" "$WS/digest" "$WS/recap"
sudo /bin/chmod 600 "$WS/atlas/operator.json"
```

These are NOT in `manifest.files[]` (and thus excluded from sync) on purpose: the
operator edits them post-install, and a sync pass would clobber operator edits.
v1 contract: declared = synced; not declared = operator territory.

**No `atlas/llm-config.json`.** Atlas routes LLM calls through the bot's gateway
via the `openclaw_headless` transport (see [docs/principle-apps-inherit-bot-llm.md](../principle-apps-inherit-bot-llm.md)).
The bot's configured Anthropic credential / tier ladder / daily_cap_usd govern
every Atlas LLM call; no per-app API key is needed.

---

## 6. Configure Atlas's per-app settings

Three config files in `/Users/atlas/.openclaw/workspace/atlas/`:

**`operator.json`** — **MUST be filled in or Atlas won't operate at all.** Edit:
```json
{
  "operator_telegram_user_id": <YOUR_USER_ID_FROM_STEP_4.5>,
  "approved_group_chat_ids": [<CHAT_ID_FROM_STEP_4>]
}
```
You can add more approved group chat IDs later if you want Atlas to operate in multiple groups.

**No `llm-config.json` to edit.** Atlas's classifier, scope-check, strategy-check, and synthesizer all route LLM calls through the bot's gateway (`openclaw_headless` transport — see [docs/principle-apps-inherit-bot-llm.md](../principle-apps-inherit-bot-llm.md)). The bot's configured Anthropic credential and tier ladder govern every Atlas LLM call. Atlas's spend rolls up into the bot's `daily_cap_usd` automatically.

**`sources.json`** — adjust the RSS feeds, GitHub repos, and Brave queries to match what you want Atlas to watch (defaults are reasonable starting points).

**`research-config.json`** — rate limits and budget cap (default: 3/hr per member, $1/day pod-wide). Adjust if needed.

After editing, verify the operator config parses correctly:

```bash
sudo -u atlas /bin/bash -c 'cd /tmp && python3 /Users/atlas/.openclaw/workspace/scripts/atlas_guard.py show --bot-id atlas'
```

You should see your user ID and the approved chat IDs printed. If you see zeros / empty lists, edit operator.json again.

---

## 7. Install AGENTS.md guidance

The bot must know how to invoke Atlas's scripts when it sees events in the group. This goes in atlas's AGENTS.md (per-bot, in the workspace).

On the mini:

```bash
# Read the guidance file from the worktree (you should have it staged)
scp /Users/pod-admin/GitHub/evolve/.claude/worktrees/elegant-bohr-f5356c/docs/atlas-app-manifests/scripts/atlas-agents-md-guidance.md pod-admin-user@mini:/tmp/

# Append to atlas's AGENTS.md (creates it if missing)
ssh pod-admin-user@mini
sudo -u atlas /bin/bash -c '
WS=/Users/atlas/.openclaw/workspace
if [ ! -f "$WS/AGENTS.md" ]; then
  echo "# Atlas AGENTS.md" > "$WS/AGENTS.md"
fi
echo "" >> "$WS/AGENTS.md"
cat /tmp/atlas-agents-md-guidance.md >> "$WS/AGENTS.md"
'
```

After this, AGENTS.md has the four `## Atlas — *` sections (Identity and tone, Article Capture, On-Demand Research, Privacy posture).

---

## 8. Install application manifests

Copy the four manifest JSONs to the pod's applications directory:

```bash
scp docs/atlas-app-manifests/atlas-*.json pod-admin-user@mini:/tmp/

ssh pod-admin-user@mini
APPS=/Users/Shared/evolve/applications/atlas
sudo /bin/mkdir -p "$APPS"
sudo /bin/cp /tmp/atlas-daily-digest.json     "$APPS/app_atlas_daily_digest.json"
sudo /bin/cp /tmp/atlas-article-capture.json  "$APPS/app_atlas_article_capture.json"
sudo /bin/cp /tmp/atlas-on-demand-research.json "$APPS/app_atlas_on_demand_research.json"
sudo /bin/cp /tmp/atlas-weekly-recap.json     "$APPS/app_atlas_weekly_recap.json"
sudo /usr/sbin/chown -R evolve:staff "$APPS"

# Verify Evolve sees them
evolve-admin application list atlas
```

---

## 9. Install the daily-digest LaunchDaemon

```bash
scp docs/atlas-app-manifests/plists/com.atlas.atlas-daily-digest.plist pod-admin-user@mini:/tmp/

ssh pod-admin-user@mini

# Substitute the time if you want something other than 07:00
DIGEST_HOUR=7
DIGEST_MIN=0
sudo /usr/bin/plutil -replace StartCalendarInterval.Hour   -integer $DIGEST_HOUR /tmp/com.atlas.atlas-daily-digest.plist
sudo /usr/bin/plutil -replace StartCalendarInterval.Minute -integer $DIGEST_MIN /tmp/com.atlas.atlas-daily-digest.plist
sudo /usr/bin/plutil -lint /tmp/com.atlas.atlas-daily-digest.plist  # must say OK

# Install
sudo /bin/cp /tmp/com.atlas.atlas-daily-digest.plist /Library/LaunchDaemons/com.atlas.atlas-daily-digest.plist
sudo /usr/sbin/chown root:wheel /Library/LaunchDaemons/com.atlas.atlas-daily-digest.plist
sudo /bin/chmod 644 /Library/LaunchDaemons/com.atlas.atlas-daily-digest.plist

# Substitute the chat-id in the cron script
WS=/Users/atlas/.openclaw/workspace
sudo /usr/bin/sed -i '' "s/{bot_id}/atlas/g; s/{telegram_chat_id}/<YOUR_CHAT_ID>/g; s/{time_zone}/America\/Los_Angeles/g; s/{detail}/standard/g" "$WS/scripts/atlas-digest-cron.sh"

# Boot the daemon
sudo /bin/launchctl bootstrap system /Library/LaunchDaemons/com.atlas.atlas-daily-digest.plist
sudo /bin/launchctl enable system/com.atlas.atlas-daily-digest

# Verify
sudo /bin/launchctl print system/com.atlas.atlas-daily-digest | head -10
```

---

## 10. Install the weekly-recap LaunchDaemon

Same pattern as step 9, with the recap plist + cron script. Default schedule is Sunday 09:00 local.

```bash
scp docs/atlas-app-manifests/plists/com.atlas.atlas-weekly-recap.plist pod-admin-user@mini:/tmp/

ssh pod-admin-user@mini

# Defaults are Sunday 09:00; adjust if needed:
# sudo /usr/bin/plutil -replace StartCalendarInterval.Hour -integer 9 /tmp/com.atlas.atlas-weekly-recap.plist

sudo /bin/cp /tmp/com.atlas.atlas-weekly-recap.plist /Library/LaunchDaemons/com.atlas.atlas-weekly-recap.plist
sudo /usr/sbin/chown root:wheel /Library/LaunchDaemons/com.atlas.atlas-weekly-recap.plist
sudo /bin/chmod 644 /Library/LaunchDaemons/com.atlas.atlas-weekly-recap.plist

WS=/Users/atlas/.openclaw/workspace
sudo /usr/bin/sed -i '' "s/{bot_id}/atlas/g; s/{telegram_chat_id}/<YOUR_CHAT_ID>/g; s/{detail}/standard/g" "$WS/scripts/atlas-recap-cron.sh"

sudo /bin/launchctl bootstrap system /Library/LaunchDaemons/com.atlas.atlas-weekly-recap.plist
sudo /bin/launchctl enable system/com.atlas.atlas-weekly-recap
```

Article-capture and on-demand-research have NO plist — they're event-driven via AGENTS.md guidance.

---

## 11. Smoke-test each app before going live

Run each manually as the atlas user. Don't add Atlas to the group until these pass.

```bash
ssh pod-admin-user@mini
cd /tmp && sudo -u atlas /bin/bash -c '
WS=/Users/atlas/.openclaw/workspace
cd /tmp  # NOT cwd-in-pod-admin-user-home, per reference_sudo_evolve_python_cwd

# 1. Daily digest — preview (no Telegram post, no archive write)
python3 "$WS/scripts/atlas_digest.py" preview --bot-id atlas --detail concise

# 2. Article capture in approved group — should classify and archive
APPROVED_CHAT=<your approved chat_id>
MEMBER_ID=<any group member user_id>
python3 "$WS/scripts/atlas_capture.py" process --bot-id atlas \
  --url "https://www.anthropic.com/news" --message-id smoke1 --member-id $MEMBER_ID \
  --chat-id $APPROVED_CHAT --chat-type supergroup

# 3. Same URL again — should report DUPLICATE
python3 "$WS/scripts/atlas_capture.py" process --bot-id atlas \
  --url "https://www.anthropic.com/news" --message-id smoke2 --member-id $MEMBER_ID \
  --chat-id $APPROVED_CHAT --chat-type supergroup

# 4. Opt-out — should delete and emit registered count >= 1
python3 "$WS/scripts/atlas_capture.py" opt-out --bot-id atlas \
  --url "https://www.anthropic.com/news" --member-id $MEMBER_ID \
  --chat-id $APPROVED_CHAT --chat-type supergroup

# 5. Research budget (no api calls)
python3 "$WS/scripts/atlas_research.py" budget --bot-id atlas

# 6. Research ask — on-topic in approved group
python3 "$WS/scripts/atlas_research.py" ask --bot-id atlas \
  --query "What is MCP in the agent context?" --member-id $MEMBER_ID --message-id m1 \
  --chat-id $APPROVED_CHAT --chat-type supergroup

# 7. Research ask — off-topic (should refuse)
python3 "$WS/scripts/atlas_research.py" ask --bot-id atlas \
  --query "What is the weather in Tokyo?" --member-id $MEMBER_ID --message-id m2 \
  --chat-id $APPROVED_CHAT --chat-type supergroup

# 8. Recap status
python3 "$WS/scripts/atlas_recap.py" status --bot-id atlas

# --- Guard layer tests (critical for security) ---

# 9. Capture from a STRANGER DM — must refuse
python3 "$WS/scripts/atlas_capture.py" process --bot-id atlas \
  --url "https://example.com/test" --message-id smoke9 --member-id 999999999 \
  --chat-id 999999999 --chat-type private

# 10. Research from a STRANGER DM — must be silent (no output)
python3 "$WS/scripts/atlas_research.py" ask --bot-id atlas \
  --query "What is MCP?" --member-id 999999999 --message-id m10 \
  --chat-id 999999999 --chat-type private

# 11. Capture from a FOREIGN GROUP — must refuse
python3 "$WS/scripts/atlas_capture.py" process --bot-id atlas \
  --url "https://example.com/test" --message-id smoke11 --member-id $MEMBER_ID \
  --chat-id -1099999999 --chat-type supergroup

# 12. Research from OPERATOR DM — must answer (bypasses rate limit)
OPERATOR_ID=<your telegram user_id>
python3 "$WS/scripts/atlas_research.py" ask --bot-id atlas \
  --query "What is MCP?" --member-id $OPERATOR_ID --message-id m12 \
  --chat-id $OPERATOR_ID --chat-type private
'
```

Expected outcomes:
- `(1)` prints a formatted digest (cost: ~$0.02 for classification)
- `(2)` ends with `CAPTURE_ARCHIVED:<bucket>`
- `(3)` ends with `CAPTURE_DUPLICATE`
- `(4)` ends with `CAPTURE_OPT_OUT_REGISTERED <url> 1`
- `(5)` prints budget zeros + today's date
- `(6)` ends with `RESEARCH_ANSWERED:<base64>`
- `(7)` ends with `RESEARCH_REFUSED:` + the off-topic template text
- `(8)` prints "no recaps posted yet"
- `(9)` prints `CAPTURE_SKIPPED:not_in_approved_group context=dm role=stranger`
- `(10)` produces no stdout output and exits 0 (silent ignore is correct)
- `(11)` prints `CAPTURE_SKIPPED:not_in_approved_group context=foreign_group role=stranger`
- `(12)` ends with `RESEARCH_ANSWERED:<base64>` (operator DMs work even though it's a private chat)

**If `(9)`, `(10)`, or `(11)` produce any other output** — especially CAPTURE_ARCHIVED or RESEARCH_ANSWERED — the guard layer is misconfigured. Most likely cause: operator.json is missing or the approved_group_chat_ids list is empty. Fix before proceeding to step 12.

If any of these fail, look in `/tmp/atlas-*.log` and `/tmp/atlas-*-err.log` for context. Don't proceed until all pass.

---

## 12. Add Atlas to the enthusiast group

Now that Atlas is verified:

1. Add Atlas to the Telegram group as a regular member (NOT admin — admin status is not required for any Atlas feature).
2. As a group admin yourself, ensure Atlas can post (some groups restrict bot posting; toggle "Send Messages" permission for atlas if needed).
3. **Post the privacy intro manually** (or DM atlas "post your introduction" and it should follow the AGENTS.md guidance).
4. Pin the intro message: long-press → Pin.
5. Send a test URL in the group. Within 30 seconds, atlas should react with a bucket emoji.
6. @-mention Atlas with a focused question. Within 30 seconds, atlas should reply in-thread with a sourced answer.
7. `/optout <url>` on a URL you just shared. Atlas should react ✅ and delete the archive entry.

---

## 13. Watch for a week

Per `Pod-Admin's workflow`: ship, use, retrospect.

Things to monitor in the first week:

- **Daily digest** — does it arrive at 07:00? Is the classification quality reasonable? Are the buckets balanced?
- **Article-capture latency** — does the bucket emoji appear within 30s of a URL post?
- **/optout flow** — does it actually delete the archive entry? Verify with `evolve-admin application status atlas`.
- **Research refusal accuracy** — are on-topic questions getting answered? Are off-topic questions actually being refused? Check `/tmp/atlas-research.log`.
- **Cost** — `python3 atlas_research.py budget` daily. Compare against the $1/day cap.
- **Member sentiment** — does the group find the digest valuable, or is it spam? React-counts on atlas's posts are a quick signal.

After the week, write a retrospective into a new memory file capturing:
- What worked
- Where classification missed (e.g. "everything got bucketed as new_tools")
- Where the privacy story held up vs. felt awkward
- Whether the manifest spec gaps from `GAPS.md` actually bit, or were theoretical
- Whether the schema v7 work (per `manifest-schema-v7-recommendation`) feels urgent now

---

## Rollback

If Atlas misbehaves and you need to disable it fast:

```bash
ssh pod-admin-user@mini
sudo /bin/launchctl disable system/com.atlas.atlas-daily-digest
sudo /bin/launchctl disable system/com.atlas.atlas-weekly-recap

# Remove from the group via Telegram (long-press the bot → Remove)
# This is enough — the bot still exists but won't post unsolicited.
```

To fully retire Atlas:

```bash
sudo /bin/launchctl bootout system /Library/LaunchDaemons/com.atlas.atlas-daily-digest.plist
sudo /bin/launchctl bootout system /Library/LaunchDaemons/com.atlas.atlas-weekly-recap.plist
sudo rm /Library/LaunchDaemons/com.atlas.atlas-daily-digest.plist
sudo rm /Library/LaunchDaemons/com.atlas.atlas-weekly-recap.plist
sudo evolve-admin retire-bot atlas  # graceful: stops daemons, archives workspace + closure summary
# Or for full removal including macOS user + /Users/atlas/:
# sudo evolve-admin delete-bot atlas
```

The bot's macOS user, workspace, archive, and logs persist after retire. To wipe completely, delete `/Users/atlas/` after confirming nothing in the archive is wanted.

---

## What this deploy did NOT do

- **It did not solve schema v6 → v7 migration.** The four manifests live with workarounds (event_triggers as a non-standard extension; AGENTS.md guidance as build_spec prose). When schema v7 lands, the manifests need updating. See [GAPS.md](GAPS.md).
- **It did not register Atlas in the gallery.** These four apps live at `docs/atlas-app-manifests/` as drafts, not at `gallery/atlas-*/`. Promotion to gallery happens after Atlas runs in production for a week or two and the patterns stabilize.
- **It did not write tests beyond smoke tests.** Each app's `test_cases` in the manifest should be wired up to evolve-admin's test runner. Wait for the first week's retrospective before investing in that — the test taxonomy may need to change.

---

## Related

- `reference_evolve_admin_cli` — CLI syntax
- `reference_ssh_mini_host` — SSH conventions
- `reference_sudo_evolve_python_cwd` — why `cd /tmp` before `sudo -u`
- `feedback_evolve_dev_not_test_pod` — fix bugs structurally, don't bypass
- [GAPS.md](GAPS.md) — manifest spec gaps surfaced by this build
- [README.md](README.md) — overview of the four manifests
