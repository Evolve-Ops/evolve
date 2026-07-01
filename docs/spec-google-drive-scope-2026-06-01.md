# Drive scope for shared-folder writes — Decision

Status: **draft**
Date: 2026-06-01
Supersedes: nothing
Amends: [docs/spec-google-integration-paths-2026-05-30.md](spec-google-integration-paths-2026-05-30.md) §6 (scope catalog) and §7 (wizard flow); [docs/runbook-path-c-google-integration.md](runbook-path-c-google-integration.md) §4.2

---

## 0. TL;DR

`drive.file` is the right default for path-C bots, but it does **not** cover the headline runbook §4.2 pattern — primary user creates a folder in their personal Drive and shares it with the bot's Workspace mailbox as Editor. Service accounts cannot use the Drive Picker, so a folder the SA didn't create and didn't pick is invisible to a `drive.file`-scoped call, even when the SA's subject has Editor on it. The current symptom is `HttpError 404: File not found: <folder_id>` when the LLM passes `parent_folder_id` to `drive_write_file`.

**Decision: Option C (hybrid).** Keep `drive.file` as the default. When the operator picks an app that needs to write into an externally-shared folder, the wizard surfaces a one-step scope upgrade to `drive` with an explicit-confirmation gate and a bot-tile flag — closing GAP-12 from the parent spec at the same time. Document the Domain-Wide Delegation impersonation alternative in §6 as a known escape hatch we are deliberately not building for v1.

---

## 1. Bug repro

Current code at [packages/admin/evolve_admin/mcp_bridge/google_tools.py:43](packages/admin/evolve_admin/mcp_bridge/google_tools.py:43):

```python
DRIVE_SCOPES_FILE = ["https://www.googleapis.com/auth/drive.file"]
```

`drive_write_file` calls `service.files().create(body={"parents": [parent_folder_id]}, ...)`. Google evaluates the parents list against the SA's `drive.file`-visible file set:

| File visible to `drive.file`? | When |
|---|---|
| Yes | The SA (acting as its subject via DwD) created it |
| Yes | A user opened it for the SA's app via the Drive Picker UI |
| **No** | Anyone shared it *to* the SA's subject (incl. Editor, Owner-transfer) |

The runbook §4.2 flow lands every shared folder in the third row. Result: 404 on the parent, write fails, the operator sees "the folder exists, the bot has Editor, why doesn't it work?"

This is not a transient or config bug. `drive.file` semantically excludes externally-shared resources for service accounts.

---

## 2. Options considered

### Option A — Make `drive` the default

Replace `DRIVE_SCOPES_FILE` with `DRIVE_SCOPES_FULL`. Update runbook §4.2 wording.

Pros: simplest, runbook works as written, "share folder → bot writes" matches operator mental model.

Cons:
- Loosens default privileges silently — a compromised bot can now read **every** file in its own Drive, not just its workspace folder.
- Contradicts the spec §6 "principle of least privilege" line that names `drive.file` as the personal-assistant default.
- GAP-12 (operator confirmation for high-privilege scopes, per spec §6) becomes load-bearing but is still unimplemented.

### Option B — Reverse the sharing direction

The bot creates the workspace folder in **its own** Drive, then shares it with the primary user as Editor. `drive.file` covers this because the SA's subject is the creator.

Pros: `drive.file` stays as default; cleanest "least privilege" story.

Cons:
- **Reframes data ownership.** Today the user's data lives in the user's Drive; bot decommission has no effect on the files. Under B, decommissioning the bot removes the files (or requires an explicit ownership-transfer step in the retire flow).
- Requires Drive to be licensed on the bot's Workspace mailbox — extra seat cost per bot. Path C's appeal today is one Workspace seat per bot for *mail*; Drive adds storage quota too.
- Migration: existing setups under runbook §4.2 need a manual "move folder to bot, re-share back" step. The migration tool would have to operate against the user's personal Google account, which path C deliberately avoids.
- Conceptually clean but breaks the §3.4 promise of "primary user keeps personal data on a personal Google account."

### Option C — Hybrid: opt-in scope upgrade — RECOMMENDED

Default stays `drive.file`. The wizard offers a per-bot upgrade to `drive` with explicit gating:

1. **App declares its access pattern.** Manifest gains `google_drive_access: "bot_owned" | "shared_folder"` (default `bot_owned`).
2. **Wizard branches at Screen 4.** When `shared_folder` is declared, the wizard shows the scope-upgrade screen — explains the trade-off in plain language (Plex test), requires a checkbox confirmation, and writes `drive` into the bot's scope set.
3. **Bot tile surfaces the elevated scope.** Same chip pattern as today's high-privilege flags; the operator always sees which bots run with full Drive.
4. **Default app set unchanged.** Personal-assistant defaults remain `drive.file`; only apps that *need* shared-folder writes opt in.

Pros: secure by default; the wizard makes the trade-off visible at the moment the operator is making the decision; satisfies spec §6 GAP-12 as a side effect; no migration of file ownership; no extra Workspace seats.

Cons: more wizard surface area than A; requires per-app declaration which not every app author will fill in correctly; the operator still ends up with `drive` when shared folders are involved (same blast radius as Option A, but informed).

### Option D — Domain-Wide Delegation user impersonation

Mentioned for completeness. Path C uses DwD to impersonate the bot's *own* Workspace mailbox today. DwD can also impersonate the *primary user* (any Workspace user the SA is authorized for). When the SA acts as the primary user, `drive.file` covers anything that user created — including folders they made and intend to share.

Pros: `drive.file` keeps working semantically; the SA literally is acting as the primary user, so it inherits the user's file visibility.

Cons:
- Only works when the primary user is *inside the Workspace*. The headline case from §3.4 (primary user on a personal `@gmail.com` account) is not covered.
- Conflates two principals (bot vs. primary user) in audit logs and in evo's mental model — every action the bot takes shows up as the primary user, which we deliberately avoided.
- Adds a second impersonation hop to reason about per bot.

Documented in §6 as an escape hatch for Workspace-internal primary users; not the path forward for v1.

---

## 3. Decision

**Adopt Option C.**

Rationale:
- Preserves the §3.4 ownership story (files live in the user's Drive, survive bot decommission).
- Keeps least-privilege as the default for the common case.
- Closes the operator-confirmation gap (GAP-12) that the parent spec already required.
- Surfaces the trade-off to the operator at the moment they're choosing, instead of either (a) silently loosening defaults or (b) silently producing a 404 they have to diagnose.
- The Plex-test user gets a wizard prompt they can understand, not a Google API error message.

---

## 4. Wizard UX sketch

Triggered when the app's manifest declares `google_drive_access: "shared_folder"`, OR when the operator manually checks the box in the Drive sub-screen of the credentials wizard.

```
Screen: Drive access

  This app writes into a folder you'll share with the bot from your
  personal Drive.

  The bot's default Drive scope ("drive.file") only lets the bot see files
  it created itself. Google does not let service accounts see folders that
  were shared with them — so the default scope won't work here.

  To make this app work, the bot needs full Drive access ("drive"):

    ✓ Bot can write into the folder you share with it
    ✓ Bot can read files inside that folder
    ⚠ Bot can also read every other file in its own Drive
    ⚠ Bot can create files anywhere in its own Drive

  This bot will show a "Full Drive" badge on its tile so you always know
  it has elevated access.

  [ ] I understand and want to grant Full Drive access to this bot.

  [Continue]  [Back]
```

Implementation notes:
- The checkbox writes `drive` (not `drive.file`) into `google_integration.scopes` for this bot.
- The wizard runs the same pre-flight as today (§7.2): asserts `drive` is in the DwD-authorized scope set, makes a `files.list(pageSize=1)` call to confirm.
- On the bot tile, add a `full-drive` chip routed through the existing high-privilege chip pipeline (same shape as the `gmail.modify` chip work-item from spec §6).

---

## 5. Runbook §4.2 rewrite

Replace the current §4.2 with:

> ### 4.2 Create + share a workspace folder in Drive
>
> 1. Open [drive.google.com](https://drive.google.com) on the primary user's account.
> 2. **New → Folder.** Name it according to the bot's purpose (e.g. `Travel`, `ProjectX`).
> 3. Right-click the folder → **Share.**
> 4. Add the bot's Workspace email.
> 5. Permissions: **Editor.**
> 6. Send.
>
> **Note on Drive scope.** If this folder is meant for an app that needs to *write* into it (most apps that use shared folders), the bot must be configured with the `drive` scope, not the default `drive.file`. The wizard will prompt for this when you add an app that declares shared-folder access. If you skip the prompt and write later fails with `404: File not found`, return to Bot Config → Credentials → Drive and check the "Full Drive access" box.
>
> *Why:* Service accounts cannot use Google's Drive Picker, so folders shared **to** the bot aren't visible under `drive.file`. The wider `drive` scope is the only way to make shared-folder writes work today. (See [docs/spec-google-drive-scope-2026-06-01.md](spec-google-drive-scope-2026-06-01.md) for the design rationale.)

Also update the "Common pitfalls" table in §5 with a row:

| Symptom | Likely cause | Fix |
|---|---|---|
| `drive_write_file` returns `HttpError 404: File not found: <folder_id>` | Bot has `drive.file` scope; the folder was shared **to** the bot rather than created by it | Upgrade the bot's Drive scope to `drive` in Bot Config → Credentials → Drive |

---

## 6. DwD impersonation as escape hatch

For Workspace-internal primary users (Sam has a `@example-corp.com` mailbox, not a personal `@gmail.com`), the SA could DwD-impersonate Sam directly. We are deliberately not wiring this up for v1 because:

- The headline case is a personal `@gmail.com` primary user, which DwD impersonation cannot reach.
- Two-principal impersonation per bot complicates audit and evo's tool surface.

If a future operator pushes for Workspace-internal user impersonation, the path forward is a per-bot `subject_override` that the wizard surfaces with the same scope-confirmation pattern as §4 above.

---

## 7. Migration

- **Existing path-C bots that don't write to shared folders.** No change. They keep `drive.file`. No-op.
- **Existing path-C bots that have been failing on shared-folder writes.** The fix is a scope edit in their config + DwD re-authorization for `drive` at the Workspace tenant. The wizard's "edit bot" flow gets the same scope-confirmation screen as §4.
- **Apps in the catalog.** Apps that today rely on shared-folder writes need `google_drive_access: "shared_folder"` added to their manifest. The default `bot_owned` keeps current behavior. Audit: grep manifests for `parent_folder_id` usage and flip the affected ones in the same PR.

---

## 8. Scope of the change

In:
- [packages/admin/evolve_admin/mcp_bridge/google_tools.py](packages/admin/evolve_admin/mcp_bridge/google_tools.py) — the scope is now config-driven, not a module constant. `drive_write_file` reads the bot's `google_integration.scopes` and picks the strongest Drive scope present.
- Wizard "Drive access" screen + the manifest schema change for `google_drive_access`.
- Bot-tile chip for `full-drive` (parallels existing high-privilege chips).
- Runbook §4.2 rewrite + §5 pitfalls table row.
- Spec §6 scope catalog: amend the `drive.file` row to note the shared-folder limitation.

Out:
- DwD user impersonation (Option D, §6).
- Reorganizing where the workspace folder lives (Option B).
- Drive scope decisions for non-folder writes (root-of-Drive uploads still work fine with `drive.file`).
- Read-access to pre-share existing files (acknowledged limitation; out of scope).

---

## 9. Open questions

1. Should the manifest `google_drive_access` be tri-state (`bot_owned | shared_folder | either`)? `either` would let apps that *could* work both ways defer the choice to the operator. Leaning no — apps usually know.
2. Does the Apps catalog UI need to surface "this app will request elevated Drive scope" before install, or is the wizard prompt at credential-setup time enough? Leaning wizard-time, but worth a second look once the wizard screen is sketched in real UI.
3. Should the `full-drive` chip be a soft warning (chip only) or a hard one (chip + Alerts page entry)? Today's high-privilege scopes show as chips only. Sticking with that for consistency unless we see operators ignoring it.
