# Google OAuth Consent Screen — Operator Runbook

**Audience:** operators configuring Evolve's Google integration paths A (free Gmail + user OAuth) or B (Workspace + user OAuth). Path C operators (service account + DwD) do not need this runbook.

**Companion spec:** [docs/spec-google-integration-paths-2026-05-30.md](spec-google-integration-paths-2026-05-30.md)

---

## Why this matters

The single largest source of broken Google integrations in Evolve is OAuth refresh-token expiration. The expiration rules are entirely controlled by the **OAuth consent screen state** of the GCP project that issued the bot's credentials. Picking the wrong state — or leaving the default state ("Testing") — means the bot's auth breaks every 7 days and the operator gets paged for a manual re-auth.

This runbook explains the three states, when each applies, and how to move between them.

---

## The three states

| State | Refresh token lifetime | Verification required? | Typical use |
|---|---|---|---|
| **Testing** | 7 days | No | Initial setup; the default after creating a new GCP project |
| **Internal** | No expiry timer (subject to 6-month inactivity + rotation limits) | No | Operator has a Workspace; users limited to the Workspace tenant |
| **Published / In production** | Same as Internal | Yes, for any sensitive scopes (Gmail, Drive, Calendar) — multi-week Google review process | Public app distributed to users outside any one Workspace |

**Default operator action:** if you have a Workspace, flip your OAuth consent screen to **Internal** immediately after creating the GCP project. This eliminates the 7-day timer with no Google review. If you don't have a Workspace, you're stuck on Testing unless you either (a) acquire Workspace, or (b) complete Google verification.

---

## How to check your current state

1. Open the [Google Cloud Console](https://console.cloud.google.com/).
2. Select your GCP project (the one whose OAuth client your bot uses).
3. Navigate to **APIs & Services → OAuth consent screen**.
4. The "Publishing status" card at the top shows the current state.
5. If "User type" shows **External**, your options are Testing or Published — Internal is unavailable to you (Internal requires the GCP project to be inside a Workspace tenant).
6. If "User type" shows **Internal**, you're set; tokens don't expire on the 7-day timer.

---

## Flipping from Testing to Internal (Workspace operators)

Available only when the GCP project is inside a Workspace tenant.

1. Open **APIs & Services → OAuth consent screen**.
2. If "User type" already shows **Internal**, no action needed.
3. If "User type" shows **External**, you cannot flip directly — you must first create the project inside the Workspace organization. To do this:
   - Confirm your GCP project's "Organization" field (top of console) is your Workspace's organization, not "No organization."
   - If it's "No organization," migrate the project to the organization (Cloud Resource Manager → Move project) OR create a new project under the organization and migrate the OAuth client.
   - Once the project is under the organization, you can change User type to Internal.
4. Save. The 7-day refresh-token timer no longer applies.

Existing OAuth tokens issued under Testing mode continue to work; you do not need to re-issue them after flipping to Internal. New tokens issued from this point inherit Internal rules.

---

## Publishing (and verification)

Published / In production is required for any app whose user base extends outside any one Workspace AND uses non-sensitive scopes. For sensitive scopes (Gmail, Drive, Calendar), publishing also requires Google's verification process.

Evolve's typical bot is single-tenant (one operator's bots, used by their own people), so publishing is rarely the right answer. The exceptions:

- A free-Gmail-only operator (path A) who wants to escape the 7-day timer permanently. They can complete verification to do so.
- An operator distributing a bot template for use by other Workspaces, each running their own Evolve. (Out of scope for the current architecture; flagged here for future reference.)

### Verification overview

If you decide to verify:

1. Move the consent screen to "In production" state.
2. Google triggers verification automatically for sensitive scopes.
3. You'll need:
   - A verified domain (you must own and verify a domain in Search Console).
   - A privacy policy URL hosted on that domain.
   - A homepage URL hosted on that domain.
   - For "restricted" scopes (e.g. `gmail.readonly`, `drive`), a third-party security assessment (CASA) — this is the multi-week part. Quotes typically run $3,000–$15,000 from approved assessors.
4. Submit through the console. Google's review takes 4–6 weeks for standard sensitive scopes, 2–3 months for restricted scopes requiring CASA.

For most Evolve operators, the path of least resistance is "acquire a Workspace + use path C (service account + DwD)" rather than verification.

---

## Scope sensitivity reference

Google classifies scopes by sensitivity. Higher sensitivity = stricter verification requirements.

| Sensitivity | Examples | Verification needed if Published? |
|---|---|---|
| Non-sensitive | `userinfo.email`, `userinfo.profile`, `openid` | No |
| Sensitive | `gmail.send`, `calendar`, `drive.file`, `contacts.readonly` | Yes (standard verification) |
| Restricted | `gmail.readonly`, `gmail.modify`, `drive`, `drive.readonly` | Yes + CASA security assessment |

Source: [Google's OAuth API verification FAQ](https://support.google.com/cloud/answer/9110914) (verify before relying on this table — Google's classifications evolve).

---

## When the wizard asks "what is your consent screen state?"

The Evolve add-bot wizard prompts for this when configuring a path-A or path-B integration. Your answer determines:

- What expiry warnings the wizard surfaces.
- What the health monitor's pre-flight check expects (e.g. if you say "Internal," the monitor will not warn about the 7-day timer; if it then sees a 7-day expiry pattern in practice, it'll flag the configuration as inconsistent with the asserted state).

Be honest about your actual state, not your intended state. If you intend to flip to Internal but haven't yet, say Testing — then flip — then update the bot's config.

---

## Troubleshooting common failures

| Symptom | Likely cause | Fix |
|---|---|---|
| Bot API call returns 401 invalid_grant ~7 days after setup | Consent screen still in Testing mode | Flip to Internal (if Workspace) or accept periodic re-auth |
| 401 after 6 months of bot inactivity | Refresh token revoked due to 6-month inactivity rule | Re-authorize via browser; consider migrating to path C |
| 401 after >50 active tokens issued for the same user/client | Token rotation limit hit; old tokens auto-revoked | Re-authorize via browser; reduce token churn |
| 403 insufficient_scope | Bot's scopes don't cover the API call | Edit the bot's `google_integration.scopes` and re-authorize |
| Verification request stuck for weeks | Standard timeline | Wait; consider switching strategy if blocking |

---

## See also

- [docs/spec-google-integration-paths-2026-05-30.md](spec-google-integration-paths-2026-05-30.md) — the architecture this runbook supports
- Google docs: [OAuth 2.0 verification](https://support.google.com/cloud/answer/9110914)
- Google docs: [Refresh token expiration](https://developers.google.com/identity/protocols/oauth2#expiration)
