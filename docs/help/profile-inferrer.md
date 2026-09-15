---
title: "Help: User Profile Inferrer"
slug: profile-inferrer
audience: public
last_reviewed: 2026-06-05
concepts:
  - profile-inferrer
  - per-bot-inference
  - dnt
  - session-end-hook
  - profile-extraction
ui_surface: null
related_specs:
  - internal/spec-user-profile-2026-05-07.md
---

# Help: User Profile Inferrer

The User Profile Inferrer keeps each bot's per-user profile current by extracting new facts from the bot's own session transcripts. It runs **inside each bot at session_end**, using the bot's own LLM credentials — never as a centralized service. Spec: `internal/spec-user-profile-2026-05-07.md`.

The inferrer writes facts directly into `~/.openclaw/profiles/<user_key>.md`. There is no per-fact approval queue: things the user has already volunteered in conversation are implicitly authorized to be remembered. The user can review, edit, or wipe the profile any time via `evo profile`.

---

## How it works

1. **Triggered per session.** OpenClaw's `session_end` hook invokes the inferrer in the bot's own process.
2. **Reads the transcript.** The hook receives the just-ended session's transcript via the OC env contract (`OC_BOT_ID`, `OC_USER_KEY`, `OC_HOME_DIR`, `OC_TRANSCRIPT_PATH`).
3. **DNT short-circuit.** If the user has `tracking_enabled: false` in their profile (set via `evo profile dnt on`), the hook exits before any LLM call. No token spend on opted-out users.
4. **LLM extraction.** One Haiku 4.5 call per session: `(transcript, current_profile) → list[FactCandidate]`. The model sees the existing profile so it doesn't propose duplicates.
5. **Confidence filter.** Per-section thresholds drop low-confidence candidates. Sensitive sections (Family, Demographics, Constraints) require 0.85+; everyday sections (Goals, Vocation, Interests, Values, Communication Preferences) accept 0.70+.
6. **Direct write.** Surviving facts append into the appropriate sections; the Audit Log gets one line per write with section, source (`inferrer`), confidence, and timestamp.

---

## Activation

The inferrer is shipped as the `user_profile_inferrer` generator at `packages/analyzer/generators/user_profile_inferrer/`. Each bot installs it as a session_end hook in its `openclaw.json` at deploy time. The hook loads the bot's own Anthropic key from `~/.openclaw/credentials/anthropic.json` — never from the pod-wide evolve-side credentials store.

If the credentials are missing, the hook logs and exits quietly (`skipped: no_llm_credentials`). The bot continues to function normally; just no profile updates that session.

---

## What it can and can't record

**Can:** durable facts the user volunteered about themselves — "works as a software engineer at a small startup", "vegetarian", "prefers short responses without bullet points", "wants to ship a novel by year-end".

**Can't (or shouldn't):**

- Facts about *other people* the speaker mentions — those go into the speaker's own profile only if relevant ("my partner is a teacher" → Family), never into the third party's profile.
- Transient state — "had a bad day", "running late". The inferrer is told to extract durable facts only.
- Anything below the per-section confidence threshold.
- Anything already in the profile (the model sees existing sections and is told not to re-extract).

---

## User control

Three surfaces to inspect or alter the profile:

- **`evo profile`** — show what's been recorded. Renders sections back to the user; nothing else.
- **`evo profile dnt on`** — opt out of profile inference. Wipes all section content + Audit Log atomically. The inferrer short-circuits on subsequent sessions until the user toggles back.
- **`evo profile dnt off`** — resume inference. Starts fresh — no backfill from past observations.

The profile file at `~/.openclaw/profiles/<user_key>.md` is also directly editable by the user; the inferrer's parser preserves user-edited content.

---

## Privacy

- Each bot's inferrer sees only its own users' transcripts. Bot A's LLM never sees bot B's data — privacy by architecture, not policy.
- The profile file is mode 600, owned by the bot user. The evolve admin server's ACL is explicitly carved out for `*.md` files in `profiles/` (see `set_evolve_read_acl` in `deploy.py`).
- The admin UI sees only a binary signal — "any user has a profile with content?" — via `~/.openclaw/profiles/.status.json` (mode 644). Profile contents are never surfaced in the admin UI.

---

## Cost

One Haiku 4.5 call per session, ~2-3K input tokens (transcript) + ~200 output tokens. Roughly $0.001 per session. For an active user with 10 sessions/day, that's ~$0.30/month per user — well within the per-bot `monthly_cap_usd` design.

To stop the inferrer entirely for a specific user: `evo profile dnt on`. To stop it pod-wide for testing: remove the session_end hook from the bot's `openclaw.json`.

---

## Failure modes

- **No credentials** — logs `skipped: no_llm_credentials`, exits 0. Bot session is unaffected.
- **DNT enabled** — logs `skipped: dnt_enabled`. No token spend.
- **bot_id mismatch** — defensive guard. Logs `skipped: bot_id_mismatch`. Should never trigger if the hook is wired correctly; if it does, refuses to write rather than risk cross-contamination.
- **Empty transcript** — logs `skipped: empty_transcript`. Common for heartbeat-only sessions.
- **LLM call fails** — logs the error, returns no facts. Next session retries naturally.
