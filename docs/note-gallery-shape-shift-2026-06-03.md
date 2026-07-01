# Note: The gallery shape shift — folder-per-app + personalization scrub

**Date:** 2026-06-03
**Status:** Architectural note (not a spec — captures the design conversation; specs follow)
**Triggered by:** docs/spec-files-pack-hybrid-2026-06-03.md (the install-cost discussion)
**Related:** docs/spec-scanned-export-2026-06-02.md (the export pipeline), docs/audit-uts-export-2026-06-03.md (the fidelity audit), docs/PLACEHOLDER_NAMING.md (the reserved-token scrub)

---

## 1 — Why this note exists

The files-pack hybrid (PR #2032 spec, #2040 / #2041 implementation) reshapes how Evolve's gallery works at install time. It also, less obviously, reshapes how an **app** is represented on disk and how **app sharing** ought to behave.

Three significant changes are in motion that should be named explicitly so we keep them coherent across the next several PRs:

1. **The gallery's publication unit moves from a single manifest file to a folder per app.**
2. **Per-bot installed manifests do NOT change** — they continue to be one JSON file per installed app, just as today.
3. **The "export an app for sharing" pipeline needs a personalization scrub step** that's distinct from the existing infrastructure-placeholder pass.

Each is explored below.

---

## 2 — Two layers, two homes

It's useful to draw a clean line between the **installed** form of an app (what lives on a particular bot's workspace) and the **shareable** form (what lives in the gallery, ready for anyone to install). They serve different purposes and they have different audiences.

| Layer | Lives in | Audience | Format |
|---|---|---|---|
| **Installed app on a bot** | `/Users/<bot_user>/.openclaw/workspace/manifests/<app_id>.json` + workspace files | This bot's LLM, this bot's operator | Single per-bot manifest + files in the workspace, both potentially containing personal data the bot has accumulated |
| **Shareable app in the gallery** | `gallery/<slug>/p-XXX.json` + `gallery/<slug>/files/` | Anyone who installs the package | Folder containing the contract (manifest with build_spec) + the canonical implementation (files-pack) — sanitised of personal data, parameterised for any install |

**The per-bot installed manifest shape is NOT changing.** Today it's a single JSON file; after F-P.7 it's still a single JSON file. The bot's workspace continues to hold the scripts, data files, and docs the app needs in whatever shape that bot uses them. The bot's data (`tasks.json`, `tag_registry.json`, custom categories, the operator's accumulated tasks) lives there and stays there.

**The gallery's shape IS changing.** Today a gallery package is one JSON file (`gallery/task-manager/p-9bfa1c84.json`) carrying the whole package — manifest plus build_spec. After F-P.7 it's a folder:

```
gallery/
└── task-manager/
    ├── p-9bfa1c84.json           # manifest with build_spec (contract layer)
    └── files/                    # canonical implementation (artifact layer)
        ├── manifest.json         # per-file metadata (modes, SHAs, placeholders)
        ├── scripts/
        │   ├── tasks.py
        │   ├── task_updater.py
        │   └── task-check.sh
        ├── TASKS.md
        └── TAGS.md
```

The contract (the manifest's build_spec) describes *what the app does*. The artifact (the files-pack) is *what the app is*, ready to deploy mechanically. Two layers, paired but separable.

---

## 3 — What this means for `build_spec`

`build_spec` doesn't go away. Its role shifts:

- **Today:** build_spec is the recipe forge uses to LLM-generate files on every install. $30+ per install.
- **After F-P.7:** build_spec is the durable description of the app. Forge regenerates files from it only when an operator explicitly asks (`--regenerate`) or when there's no `files/` yet (a brand-new package that hasn't been snapshotted yet). $0 per default install.

The build_spec remains the *source of truth* — it's what:
- The deriver produces from scanned source code
- The scanned-export pipeline shapes into a gallery candidate
- Forge consumes for genuinely-custom installs
- Improvement runs evolve through natural language

The `files/` directory is the *artifact* — a deployable snapshot of what the build_spec produces when forged once against a clean reference bot. The pairing is intentional: contract ↔ artifact.

---

## 4 — The export-sharing challenge: two kinds of placeholders

The user's observation that surfaces this section's content:

> If [an operator] has a protein tracker app, it may have reference to [their name] in the app files themselves. So part of export would be to expunge personal information and replace with generic information and variables (like "user name"). (So whoever installs the protein app will be able to replace "user name" with their name.)

This identifies a real gap that the current placeholder system doesn't cover. The placeholders we have today are all **infrastructure** — they describe the bot, not the human:

| Today's placeholders | What they parameterize |
|---|---|
| `{bot_id}` | The bot's identifier in `network.json` |
| `{bot_user}` | The macOS account the bot runs as |
| `{workspace}` | Absolute path to the bot's workspace |
| `{shared_dir}` | `/Users/Shared/evolve` |
| `{pkg_id}` | The package id |
| `{app_id}` | The application id |
| `{installed_at}` | ISO timestamp at install time |

What the protein-tracker example needs is a **personalization** layer:

| New placeholders (proposed) | What they parameterize | Resolution source |
|---|---|---|
| `{user_name}` | The human's preferred name (e.g. "Alex", or "the operator") | Per-bot profile / USER.md |
| `{user_handle}` | Short handle for output formatting (e.g. "@alex") | Per-bot profile |
| `{user_locale}` | Date/time/units convention (en-US, metric, etc.) | Per-bot profile |
| `{user_email}` | When the app sends email-style messages | Per-bot profile |

The resolution source is different: infrastructure placeholders come from `network.json` and the install context; personalization placeholders come from the bot's per-user profile (the same profile the `user_profile_inferrer` builds passively, per memory note "user profile grows passively").

### Why this matters at *export* time, not just install time

When an operator exports an app for sharing — whether via the scanned-export pipeline (PRs #2005-#2010), the F-P.3 snapshot CLI, or a future "share to community gallery" affordance — the operator's personal data is *in the files*. A protein-tracker captured by snapshot will literally contain the operator's name in:
- Greeting strings ("Good morning, Alex")
- Goal annotations ("Alex's target: 150g/day")
- Sample data ("Yesterday Alex had two eggs")
- Comments ("// alex asked for this feature")
- Maybe even hardcoded paths or app-specific config

The reserved-token scrub guard catches a specific public-launch concern (operator's pod-specific bot ids leaking into the open-source repo). It does NOT catch personal-data leakage at *gallery export time* — that's a different audience (whoever installs my protein-tracker on their pod), a different threat (privacy + portability), and a different remediation (substitute, don't reject).

### The export pipeline needs a personalization scrub step

A future PR — call it **F-P.8** in the files-pack series — adds a **personalization scrub** as an explicit stage of the export pipeline:

1. **Detect**: scan the snapshotted files for tokens that look like personal data. Sources of suggestions:
   - The operator's name from the bot's USER.md
   - Email addresses with the operator's domain
   - Recurring proper nouns that appear in app-emitted text (greeting strings, sample tasks)
   - Numeric anchors that look operator-specific (a phone number, a specific goal value)
2. **Suggest**: emit a per-file "candidate substitutions" list for operator review. Same shape as F-P.3's auto-detected infrastructure substitutions.
3. **Operator review**: each candidate is shown with three options:
   - Substitute with a placeholder (e.g. `{user_name}`)
   - Keep verbatim (it's the app's intent — e.g. a fixed enum value)
   - Redact (replace with a sample value, e.g. "User" → "Alex" for example purposes)
4. **Commit**: the chosen substitutions become part of the files-pack metadata's `placeholders[]` list, with the same install-time substitution semantics as infrastructure placeholders.

This is a small mechanism layered on F-P.3's existing auto-detection, not a separate parallel pipeline. The export tool runs both scrub passes (infrastructure + personalization) and combines the findings.

### What the personalization placeholder system needs from the platform

To make `{user_name}` etc. resolve correctly on the consumer side, the install context needs to grow:

- **`resolve_install_context`** (F-P.1) needs new optional fields for user-profile-derived values.
- The **per-bot profile** (already exists, populated by `user_profile_inferrer`) becomes the canonical source for these values. Each bot's profile is the answer to "who is this bot's primary user?"
- **`KNOWN_PLACEHOLDERS`** grows to include the personalization names.
- The files-pack format version bumps (`format_version` from 1.0 to 2.0) when the personalization placeholders land — old consumers refuse them gracefully via the existing warn-on-unknown-version code path.

---

## 5 — Migrating the existing gallery

The current gallery has 13 packages, none of which have files-packs:

| Slug | Where to snapshot from (initial guess) |
|---|---|
| task-manager | atlas (clean v17 install verified 2026-06-03) |
| unified-task-system | personal-bot reference account (hand-finished install) |
| ea-pack | personal-bot reference account |
| contacts | personal-bot reference account (likely) |
| journal | personal-bot reference account (likely) |
| workspace-backup | atlas (it has its own digest setup) — verify it has this |
| calendar-sync | personal-bot reference account (the bot with calendar OAuth) |
| email-integration | same |
| github-integration | the personal-bot reference account or atlas |
| morning-briefing | personal-bot reference account |
| note-taker | personal-bot reference account |
| calendar-summary | personal-bot reference account |
| email-triage | personal-bot reference account |

The migration plan per spec §10 of the files-pack hybrid spec:

1. For each gallery package, snapshot from the chosen source bot via F-P.3.
2. Review the auto-detected placeholders + files-pack contents (per-file `manifest.json`).
3. **NEW step (between F-P.3 and F-P.5):** run the personalization scrub (F-P.8 once it lands; until then, hand-review each file for personal data).
4. Update the gallery's `<pkg_id>.json` to add the `files_pack` metadata.
5. Commit + open a PR per migration.

Each migration PR is small (one app's files + manifest update). The end-state is the gallery directory tree shown in §2.

---

## 6 — Implications for the "share an app you made" workflow

Today the framework's app-sharing story is implicit: operators don't share apps; the gallery is curated by the project maintainers. With the snapshot CLI (F-P.3) the basic plumbing is in place for an operator to *create* a shareable package from their own bot.

After the gallery shape shift + personalization scrub, the story becomes coherent enough to surface to operators:

1. **You build an app on your bot** (via forge, or by writing the scripts yourself).
2. **`evolve-admin snapshot-files-pack --bot <yours> --pkg <pkg>`** captures it.
3. **`evolve-admin personalization-scrub <files-pack>`** (F-P.8) walks you through "is this personal, generic, or sample data?" prompts for every detected candidate.
4. The result is a sanitised files-pack you can:
   - Commit to your fork of the evolve repo's gallery
   - Submit to upstream as a contribution
   - Publish to a community gallery (when that exists)

This is the user's protein-tracker scenario, end-to-end. The operator doesn't have to know which strings in their script are personal — the scrub asks them. They don't have to know what placeholder names to use — the scrub suggests them from `KNOWN_PLACEHOLDERS`. They don't have to know how forge consumes the result — the contract layer (build_spec) and the artifact layer (files-pack) are paired by the publish step.

---

## 7 — Open design questions (recorded for later)

1. **Multi-version apps in one app folder.** A package upgrades from v1.4 → v1.5. Does `files/` get overwritten (latest wins, older versions are manifest-only) or do we maintain `files-2026.06.03-1.4/` + `files-2026.06.03-1.5/` (versioned)? **Lean: latest-wins for v1; versioned only if/when a real install-a-specific-past-version case arises.**

2. **Apps with no files (100% scheduled_actions).** Some packages are dependency-glue / cron-only. Options: ship empty `files/manifest.json`, or skip `files/` entirely. **Lean: skip entirely — `find_files_pack_dir` already returns None gracefully, and LLM-forge has nothing to do when there are no `manifest.files[]` entries.**

3. **Naming for operator audience.** Internally we use "files-pack" (specific to the per-file metadata + substitution engine). Externally — UI, runbooks, operator chat — it's probably cleaner to say "app folder" or "the package's files." **Lean: rename in operator-facing surfaces only; keep technical term in code.**

4. **`gallery/index.json` as a generated artifact.** Once every app is a folder with a canonical manifest at a predictable path, `index.json` can be built by walking `gallery/*/p-*.json`. **Defer until F-P.7 is done; the index manually-curated workflow is fine until then.**

5. **What about per-bot data in files-pack format?** If a bot has accumulated a 200-task `tasks.json`, that's not part of the gallery contract — it's the operator's data. The files-pack's `data_paths`-style classification (today: cloud/local/ephemeral) needs to be honored at snapshot time: data files don't get captured. **F-P.3 today copies the file list from the bot's installed manifest, which already excludes data paths in most cases. Worth verifying as we run migrations.**

---

## 8 — Sequence of work

Already in flight:
- ✅ **F-P.1** (#2040, merged) — foundation library
- ⏳ **F-P.2 + F-P.3** (#2041, open) — install dispatcher + snapshot CLI

Next, in order:
- **F-P.4** — integrity test + orphan-pattern linter (scrub for stale bot tokens in committed files-packs)
- **F-P.5** — first gallery republish (task-manager v1.5 with files-pack from atlas)
- **F-P.6** — operator runbook for the new install + snapshot workflow
- **F-P.7** — migrate the other 12 gallery packages (one PR per app, for reviewability)
- **F-P.8** — personalization scrub (the protein-tracker scenario; lands after the gallery migration so we have a concrete corpus to test against)

Each PR is small and reviewable. The whole sequence is probably 15-20 PRs over a few weeks. The end state is the framework's app architecture admitting what it actually is: a contract paired with a deployable artifact, separable for sharing.

---

## 9 — What does NOT change

For the avoidance of doubt:

- **Per-bot installed manifests**: still one JSON file at `workspace/manifests/<app_id>.json`. Format unchanged.
- **Per-bot workspace structure**: still has `scripts/`, `manifests/`, optional `archive/`, etc. The files-pack writes into this structure; it doesn't replace it.
- **`build_spec` field**: still part of the manifest. Still the source of truth.
- **Scanned-export pipeline (PRs #2005-#2010)**: still emits a manifest with build_spec. After F-P.8 it ALSO emits a files-pack with personalization-scrubbed candidates.
- **Forge improvement runs**: still use the build_spec to LLM-generate. After F-P.7 the operator can choose to either update the gallery's files-pack from the improved version (`--regenerate-files-pack`), or keep the improved code as a per-bot variant that doesn't flow back to the gallery.
- **Operator-personal-customization** of an installed app: the per-bot installed manifest can have categories, prefixes, tag aliases, etc. that differ from the gallery's defaults. Nothing about the shape shift constrains this.

The shape shift is strictly about the **gallery's representation** and the **export-for-sharing workflow**. The bot-side experience is untouched.
