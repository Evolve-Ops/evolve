# Integration discovery — design pass

**Status:** shipped 2026-05-05 (phases 1, 1.5, 2, 2.5, 3 all merged; v2 flag default-on as of 2026-05-05 with a kill-switch escape hatch + dotenv rotation extended to slack/telegram/discord)
**Date:** 2026-05-04 (approved); 2026-05-05 (phase 3 landed)
**Survey data:** [integration-discovery-survey-2026-05-04.json](integration-discovery-survey-2026-05-04.json)

## Approved decisions (2026-05-04)

Two foundational calls that drive the rest of the design:

**A. Split Gemini and Google Workspace.** They are different integrations with
different auth models, lifecycles, and operator concerns. The dashboard had been
conflating them under a single "Google" label. After this work:
- Gemini API access lives in the **LLM Providers** section as `Google Gemini`,
  rendered the same as Anthropic / OpenAI / xAI keys.
- Google Workspace lives in its own **Workspace** section row, which represents
  *only* user-OAuth or service-account access to Gmail / Drive / Calendar / Docs /
  Sheets / Slides — never the Gemini API key.

**B. No affordance may break a working integration.** Every action button rendered
on the dashboard has to be safe for the storage shape it appears next to. A button
that writes tokens to a location the running integration doesn't read is forbidden,
even if the row would visually look "fixed" afterward. This is the rule that forced
us toward per-probe affordances: the wizard's Reauthorize button is *correct* on a
wizard-managed row and *wrong* on a plugin-managed row, so the same row can't have
the same buttons. Probes declare what's safe; the frontend renders only what was
declared.

## Operating principle: positive evidence required

A probe surfaces an integration only when it sees **positive evidence of credentials**,
never when it only sees an enabled flag or a routing reference. `plugins.entries.google
= {enabled: true}` and `channels.slack = {...}` are intent signals; they do not by
themselves indicate the integration works. This is what would have caused security-bot to
falsely show as Workspace-connected if we'd shipped naively, and is the reason the
operator-perception-mismatch failure mode is in the catalog.

## Why this exists

Evolve's value at scale is helping operators manage *countless* OpenClaw instances, each
with its own historical config sediment. Today the dashboard's keys API has hardcoded
provider-by-provider discovery: "if google_workspace, look at auth-profiles + .config/gws".
Every new legacy storage shape we discover (Team-Bot-A's missing `.enc` file, Team-Bot-C's
plugin-managed `workspace/credentials/` dir) requires a custom branch in the keys API.

That doesn't scale. We need a discovery layer that's pluggable — register a probe per
storage pattern, surface what each probe finds, let the operator see both the canonical
config and any legacy sediment they may want to clean up.

This document catalogs the integration patterns observed across six bots in the live pod
and proposes the probes architecture grounded in that data.

## Survey: what's actually out there

Six bots × six services. Cells show the *primary* integration shape we found. "—" means
no evidence. "macOS app only" means consumer Dropbox is installed but no API integration.

| Bot    | git | dropbox | telegram | slack | discord | google (Workspace) |
|--------|-----|---------|----------|-------|---------|-------------------|
| team-bot-a    | gh CLI + gitconfig + 10 repos | macOS app only | plugin disabled + .env | plugin enabled + channel + .env | — | `~/.config/gws/` (newer cli, `credentials.json`) |
| team-bot-b   | 2 repos, no auth artifacts | macOS app only | — | — | channel block | — |
| team-bot-c  | ssh keys (id_ed25519_team-bot-c) + gh CLI + gitconfig | ranch scripts (no api) | channel block | channel block | — | `~/.openclaw/workspace/credentials/` (plugin pattern, OAuth + service account) |
| admin-bot  | gitconfig + 3 repos | macOS app only | plugin enabled + channel | channel block | — | `~/.config/gws/` (older cli, `credentials.enc`) |
| security-bot | gh CLI + 1 repo | macOS app only | channel block | — | — | none (Gemini API key only — distinct from Workspace) |
| personal-bot  | — | — | channel block | — | — | — |

Across this set, three things jumped out:

1. **"Google" is two integrations.** Every bot with `google:default` in
   auth-profiles has *Gemini API* access (an LLM key), not Google Workspace. The
   dashboard renders these in the same card. They're different integrations with
   different auth models, lifecycles, and operator concerns. Survey shows
   security-bot has Gemini but no Workspace; the dashboard today wrongly suggests security-bot has
   a Google Workspace integration too.
2. **Each integration has 2-4 distinct storage layouts.** Dropbox: zero (none of the
   bots actually integrates the Dropbox API; they use the macOS app). Slack: at least
   three (auth-profiles token_pair, workspace `.env`, channel-only routing without
   credentials). Google Workspace: three (`auth-profiles.json` wizard profile,
   `~/.config/gws/`, `~/.openclaw/workspace/credentials/`). GitHub: at least four
   (PAT in gitconfig, ssh keys, gh CLI, macOS keychain helper).
3. **Configuration is multi-layered.** A bot with Slack can have:
   - A plugin enabled flag (`plugins.entries.slack.enabled`)
   - A channel block (`channels.slack`) for routing
   - Credentials somewhere (auth-profiles, .env, encrypted store)
   - All of these can be in different states. Team-Bot-A has the plugin enabled and a .env
     hit, but no auth-profiles entry — meaning the .env is the single source of truth
     for tokens. The dashboard currently surfaces only the auth-profiles layer.

## Storage shapes we observed

Numbered for reference in the probe catalog below. These are *patterns*, not bot-specific
hacks; we expect to see them again on other instances.

### S1 — `auth-profiles.json` named entry

Wizard-managed and CLI-managed credentials live here. Profile id is the key; value is a
shape `{provider, type, key | bot_token | refresh_token | ...}`. Examples in survey:
`anthropic:api`, `brave_api_key`, `google:default` (Gemini API), `google_workspace_<bot>`
(would-be wizard entry, not seen on these bots).

This is the **canonical store**. New flows write here. Discovery is trivial: walk the
profiles dict.

### S2 — `~/.config/<service>/` directory written by external CLI

A third-party CLI (e.g. `@googleworkspace/cli`) writes its own creds dir under the
bot's home. File layouts vary by CLI version. Observed for Google Workspace on team-bot-a
and admin-bot. Discovery: list-directory probe with file-shape recognition.

Subtlety: the file *names* aren't stable across versions (`credentials.enc` vs
`credentials.json`). Probes must accept both, and ideally identify the file by content
shape (the `installed` or `web` key in the JSON marks an OAuth client_secret regardless
of filename).

### S3 — `~/.openclaw/workspace/credentials/` directory written by a plugin

Custom integrations (Team-Bot-C's ranch plugin) bundle their credentials here. Mixed
contents: OAuth client secrets, OAuth token caches, service-account JSONs, even
service-specific `.env` files (`slack.env` lives next to Google credentials). Discovery
must enumerate and *classify* each file, not assume a fixed file list.

### S4 — `~/.openclaw/workspace/manifests/<integration>.json`

Bot-authored declarative descriptions of integrations the bot uses at runtime
(`google_integration.json`, `gmail_fetcher.json`). These don't carry credentials but
*do* carry intent — they prove the bot's runtime expects the integration to work.
Useful for the dashboard to identify *which capabilities* a bot actually exercises,
beyond just "creds present somewhere".

### S5 — `~/.openclaw/workspace/.env` (or other env files)

Plain key=value files holding tokens. Team-Bot-A uses this for both Slack and Telegram tokens
(plugin enabled flags only — actual tokens in env). Discovery must read but never
*expose* contents (env files commonly hold many secrets, the probe should grep for
provider-specific keys without leaking adjacent unrelated values).

### S6 — `openclaw.json plugins.entries.<provider>`

Plugin enabled / disabled flag, sometimes with config. Examples: `plugins.entries.slack
= {enabled: true}`, `plugins.entries.google = {enabled: true}`, `plugins.entries.telegram
= {enabled: false}`. **This indicates intent, not credentials.** Useful as a signal but
not a discovery target on its own.

### S7 — `openclaw.json channels.<provider>`

Routes messages to/from a chat service. Often references credentials by profile-id but
*can* contain inline config (per existing token_pair handling in the keys API).

### S8 — System-level GitHub auth (`~/.config/gh/`, `~/.gitconfig`, `~/.ssh/`, macOS keychain)

Distinct because GitHub auth predates and lives outside any OpenClaw config. Already
partially handled by the existing `integration_token` credential class, but the survey
shows operators use *combinations* (gh CLI + ssh keys + credential helper all on the
same bot for different access patterns).

### S9 — Service-account JSON (Google Cloud)

Distinct auth model from user-OAuth. No refresh-token-revoke cycle, no per-user
consent — just a long-lived key file. Usually scoped to a specific GCP project. Found
on team-bot-c. Cannot be migrated to user-OAuth without operator intent (different access
patterns, different scopes, different audit trail).

## Proposed architecture: probes

### Concept

A `Probe` is a small function that looks in *one specific place* and returns either
`None` (no evidence found) or a structured `ProbeResult`. Each provider registers a
list of probes. The keys API runs all probes for a provider, collects results, and
renders.

```python
@dataclass
class ProbeResult:
    probe_name: str           # "wizard", "legacy_oc_gws_cli", "workspace_credentials"
    flavor: str               # human-readable: "wizard-managed", "legacy CLI", "plugin"
    confidence: Literal["confirmed", "inferred"]
    auth_model: Literal["api_key", "oauth_user", "oauth_app", "service_account",
                        "ssh_key", "credential_helper", "env_var", "unknown"]
    account: str | None       # email / username / org if known
    scopes: list[str]         # if applicable
    capabilities: list[str]   # ["gmail.read", "drive.write", ...] when known
    storage_locations: list[str]  # filesystem paths or "auth-profiles.json#<key>"
    affordances: list[Affordance] # what the operator can do with this discovery
    extras: dict              # probe-specific data for templates / future probes

class Affordance(Enum):
    REAUTHORIZE_VIA_WIZARD = "reauthorize_via_wizard"
    DISCONNECT = "disconnect"
    VIEW_CONFIG = "view_config"        # plugin-managed: show the openclaw.json fragment
    EDIT_PLUGIN = "edit_plugin"        # open the plugin config in the dashboard
    ROTATE = "rotate"                  # api-key style, in-place rotation
    MIGRATE = "migrate_to_wizard"      # legacy CLI → wizard-managed
    NONE = "external_only"             # service account, ssh keys: out of band
```

### Provider × probe registry

```python
PROBES: dict[str, list[Probe]] = {
    "google_workspace": [
        WizardAuthProfilesProbe(profile_id_fn=lambda b: f"google_workspace_{b}"),
        LegacyOcGwsCliProbe(),       # ~/.config/gws/
        WorkspaceCredentialsProbe(   # ~/.openclaw/workspace/credentials/
            recognizers=[
                OAuthClientSecretRecognizer(),
                OAuthTokenCacheRecognizer(),
                ServiceAccountJsonRecognizer(),
            ],
        ),
        WorkspaceManifestProbe(filenames=["google_integration.json", "gmail_*.json"]),
    ],
    "slack": [
        AuthProfilesTokenPairProbe(provider="slack"),
        DotenvProbe(provider="slack", env_paths=[".openclaw/workspace/.env"]),
        WorkspaceCredentialsProbe(filename_pattern="slack.env"),
        OpenclawChannelsBlockProbe(provider="slack"),  # routing-only: no creds
    ],
    "github": [
        IntegrationTokenProbe(),         # existing PAT / credhelper handling
        SshKeyProbe(),                   # ~/.ssh/id_*
        GhCliProbe(),                    # ~/.config/gh/hosts.yml
        GitconfigCredentialHelperProbe(),
        MacOsKeychainProbe(),            # opaque — only signal "helper points to keychain"
    ],
    "dropbox": [
        AuthProfilesProbe(provider="dropbox"),
        WorkspaceCredentialsProbe(filename_pattern="dropbox*"),
        # NB: macOS Dropbox app presence is NOT a probe — it's not an api integration
    ],
    # ...
}
```

### Status computation

When multiple probes hit, the row reports **all matches** as evidence chips, with the
*winning* probe's affordances controlling the action buttons. Winner is determined by
a priority order per provider (wizard > legacy CLI > plugin), but the row shows enough
to expose drift.

Example, team-bot-c's Google Workspace row after the new architecture:

> **Google Workspace · ✅ Authorized (plugin-managed)**
> Account: ranch-ops@…
> Service account + user OAuth (token.json)
> Storage: `~/.openclaw/workspace/credentials/`
> Manifest: `google_integration.json` (gmail · drive · sheets)
> Plugin: `plugins.entries.google.enabled = true`
> Actions: \[View config\] (no Reauthorize: plugin manages this)

### Affordance: why this matters

The current dashboard renders Reauthorize+Disconnect for every active row. For team-bot-c
that's actively wrong — clicking Reauthorize starts a wizard flow that writes to
`auth-profiles.json`, which the ranch plugin doesn't read. The row would flip green,
but the ranch plugin would silently keep using the old `workspace/credentials/` and now
have *two* sets of tokens.

Each probe declares which affordances make sense for its discovered shape:

- `WizardAuthProfilesProbe` → REAUTHORIZE_VIA_WIZARD, DISCONNECT
- `LegacyOcGwsCliProbe` → MIGRATE (Reauthorize through wizard, then sweep `.config/gws/`)
- `WorkspaceCredentialsProbe` → VIEW_CONFIG, EDIT_PLUGIN
- `IntegrationTokenProbe` → ROTATE
- `ServiceAccountJsonRecognizer` → NONE (managed in GCP console, out of band)

The frontend renders action buttons from the probe's affordance list, not from a
hardcoded `if (isGoogle)` branch.

## Migration plan

Three phases, each independently shippable.

### Phase 1 — Refactor existing discovery into probes (no behavior change) ✓ shipped

Moved the current `_KEY_REGISTRY`-driven logic into `WizardAuthProfilesProbe`,
`AuthProfilesTokenPairProbe`, `IntegrationTokenProbe`, `_detect_legacy_gws` →
`LegacyOcGwsCliProbe`. Keys API renders the same JSON shape it did before.

A pure refactor with one observable difference: `oc_only` / `legacy_token_age_days`
moved into the generic probe-result schema. Frontend stayed compatible via shimming.

**Shipped in [#721](https://github.com/evolve-ops/evolve/pull/721).**

### Phase 1.5 — Split Gemini and Google Workspace ✓ shipped

Renamed the `google_workspace` registry display, added a `google_gemini` LLM-section
row, made the keys API return them as separate rows. Pure registry surgery + frontend
rendering.

**Shipped in [#722](https://github.com/evolve-ops/evolve/pull/722).**

### Phase 2 — Add the missing probes for known patterns ✓ shipped

Implemented `WorkspaceCredentialsProbe`, `WorkspaceManifestProbe`, `DotenvProbe`,
`SshKeyProbe`, `GhCliProbe`. Surfaced findings on the dashboard as additional evidence
chips. Targeted button-suppression plumbing (`oc_only_no_buttons`) was added as a
stopgap while Phase 3's affordance routing landed.

**Shipped in [#723](https://github.com/evolve-ops/evolve/pull/723), behind the
`integrations.discovery.v2` flag (default off at ship time; flipped to
default on once Phase 3 affordance routing landed and slack/telegram/
discord rotation across all storage shapes was verified).**

### Phase 2.5 — Telegram bot_token rotation across storage shapes ✓ shipped

Added `OpenclawChannelsTokenProbe` so telegram/slack bots whose tokens live only in
`openclaw.json#channels.<provider>` (the live-pod case for 4 of 5 telegram-using bots)
get a working Rotate button. Storage chip surfaces where the rotate path will write.

**Shipped in [#724](https://github.com/evolve-ops/evolve/pull/724).**

### Phase 3 — Affordance routing + plugin-config view ✓ shipped

Wired the per-probe affordances into the frontend via a generic `_renderActions(row)`
helper. The keys API now attaches `actions[]` to each row, derived from the winning
probe's declared affordances. No more `if (isGoogle)` branches in the OAuth
action-rendering path. Removed the `oc_only_no_buttons` stopgap from Phase 2; it's
expressed implicitly now as "the winning probe declared `VIEW_CONFIG` only".

The "View config" affordance opens a read-only modal showing the relevant
`openclaw.json` fragment, with secret fields masked server-side. Backend endpoint:
`GET /api/admin/keys/<bot>/<provider>/config` returns
`{path, json_fragment, masked_fields}`. "Edit plugin" remains reserved for Phase 4.

## Resolved questions

**Q1 — Dashboard page split: Workspace vs LLM-Google.** **Resolved: split.**
Gemini lives under LLM Providers; Workspace gets its own Workspace section row. See
"Approved decisions" section A above.

**Q3 — `plugins.entries.google` ambiguity.** **Resolved: positive-evidence rule
governs.** Probes do not surface a Workspace integration based on
`plugins.entries.google = {enabled: true}` alone. They require credentials at one of
the catalogued storage shapes (S1, S2, or S3). Security-Bot's `plugins.entries.google` is
*not* a Workspace probe target; it'll be read by the new Gemini probe (see Phase 2)
as evidence the bot's runtime can call Gemini, alongside the API key in
`auth-profiles.json`. Phase 2 includes confirming via runtime inspection that
`plugins.entries.google` does in fact gate Gemini-only API access in OpenClaw.

## Still open (don't block implementation)

**Q4 — Multi-instance operator surface.** The probes data model already supports
cross-instance aggregation (each `ProbeResult` carries `bot_id` + `flavor` + storage
locations; aggregating across instances is just a different `iter` over the same data).
Defer the operator-facing aggregation UI; the data plumbing is correct.

## Resolved

**Q2 — Manifest signal weight (intent-without-credentials warning). Resolved
2026-05-05.** A `MANIFEST_CATALOG` (provider → fnmatch filename patterns)
lives in `packages/admin/evolve_admin/web/probes/__init__.py`. After all
probes for a provider have run, the keys-API renderer iterates rows and —
when a row's status is "missing" AND `~/.openclaw/workspace/manifests/`
contains a file matching the provider's catalog — emits a
`{kind: "manifest_without_credentials", manifests: [...], reason, remediation_hint}`
entry into the row's `warnings: [...]` array. The frontend chip already
introduced for Q5 distinguishes the `kind` field with a different short-label
("⚠ Intent without credentials" vs the Q5 read-error label) but keeps the
informational-yellow chip styling. Single chip when the only warning is
manifest-without-creds; mixed-kind rows show both.

The check is a cross-probe assertion (no probe owns it) so the catalog can
be extended without per-probe surgery. Catalog ships with Google Workspace
patterns covering the survey's observed manifests (`google_integration.json`,
`gmail_*.json`, `drive_*.json`, etc.); add new providers as new manifest
names surface — speculative patterns are explicitly avoided.

Logging: `_log.info("manifest-without-credentials: bot=<bot> provider=<prov>
manifests=<names>")` fires once per affected row per render so operators
can grep the admin-ui log even on instances that haven't reloaded the
dashboard.

**Q5 — Probe failure surface. Resolved 2026-05-05.** Probes return one of three
states: `MATCH(result)`, `NO_EVIDENCE`, or `ERROR(reason)`. The dashboard now
renders an `ERROR` as a yellow warning chip on the row ("⚠ Couldn't read
`~/.config/gws/token_cache.json`: Permission denied") instead of silently
treating it as no-evidence. This avoids the Team-Bot-A-and-Team-Bot-C failure mode where a
silent error looked identical to a clean "not configured" state.

Implementation: each `sudo /bin/cat` / `/bin/ls` helper distinguishes
"file doesn't exist" (genuine NO_EVIDENCE) from "couldn't read" (permission
denied, sudo timeout, malformed JSON) using stderr classification
(`_classify_sudo_failure`). Probes thread an `errors_out` accumulator into
each helper call; if a probe finds no positive evidence but the helpers
recorded read failures, the probe returns ERROR and the keys API attaches
the reason to the row's `warnings: [...]` list. Permission errors carry
a `remediation_hint` pointing at `evolve-admin install-infra-jobs`
(refresh-sudoers). Multiple probes per provider may error; each gets its
own entry. Even rows with `status="active"` carry warnings from sibling
probes that errored.

## Recommendation (historical — all shipped 2026-05-05)

1. **Phase 1.** Pure refactor: existing discovery moved into probes; same JSON shape
   returned by the keys API; same UI behavior. Set the foundation. **Shipped #721.**
2. **Phase 1.5: Gemini split.** Renamed the `google_workspace` registry row's display,
   added a `google_gemini` LLM-section row, made the keys API return them as separate
   rows. **Shipped #722.**
3. **Phase 2: missing probes behind a flag.** `WorkspaceCredentialsProbe`,
   `WorkspaceManifestProbe`, `DotenvProbe`, `SshKeyProbe`, `GhCliProbe`. Default off;
   flipped on per-instance once verified. **Shipped #723.**
4. **Phase 2.5: openclaw_channels rotation.** Live-pod fix for the telegram/slack
   bots whose tokens live only in `openclaw.json`. **Shipped #724.**
5. **Phase 3: affordance routing + view-config modal.** Frontend renders action
   buttons from the probe's declared affordances; new `actions[]` field on every
   row; View-config modal for plugin-managed rows. **Shipped 2026-05-05.**
6. **Q5: probe error surface.** Helpers classify sudo stderr to split "missing"
   from "couldn't read"; probes that have unread storage but no positive match
   return ERROR; the keys API attaches `warnings: [...]` to the row; the
   dashboard renders a yellow ⚠ chip next to the status badge. **Shipped
   2026-05-05.**
7. **Q2: manifest-without-credentials warnings.** `MANIFEST_CATALOG` +
   cross-probe renderer pass attaches a typed
   (`kind: "manifest_without_credentials"`) entry to a missing row's
   `warnings: [...]` when the bot's workspace declares the integration
   via a matching manifest. Distinguished short-label on the chip
   ("Intent without credentials") keeps Q5 read-error chips separable.
   **Shipped 2026-05-05.**

## What's NOT in this design

- A new credential-storage format. Existing storage shapes stay where they are; this
  is a discovery layer, not a migration tool.
- Bot-runtime changes. Plugins continue to read whichever credential location they
  were written for. Migration *between* shapes is a separate effort.
- Multi-tenant secrets handling. Today everything is per-bot, per-host. Adding any
  shared-credentials story is out of scope.
- A probe DSL. Probes are Python classes with a clear interface; we don't need a
  YAML/config-driven probe registry until we have third-party plugin authors who
  need to register their own.
