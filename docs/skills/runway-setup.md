# Setting Up Runway

The Runway skill gives your bot access to Runway's generative AI video and image generation APIs. The bot can generate videos from text prompts, apply motion effects, and create images using Runway's Gen series models.

**Install via:** Skills page → Runway → Install

*Runway appears in the credential registry (`packages/admin/evolve_admin/web/server.py` — `_KEY_REGISTRY` entry `("runway", "api_key", "API key", "api_key", "media")`, line 10330). Its credential is an API key managed through Plugins → Credentials.*

---

## What it does

After setup, the bot can:
- Generate videos from text prompts (Gen-3 Alpha, Gen-2)
- Apply motion to still images (Image-to-Video)
- Generate images from text
- Run other Runway model endpoints

---

## Prerequisites

- A Runway account (`runwayml.com`)
- An API key from the Runway dashboard
- API credits (Runway charges per generation)

---

## How to get a Runway API key

1. Log in at `runwayml.com`
2. Go to **Account settings** → **API keys**
3. Click **Create API key**, give it a name (e.g., "Evolve bot")
4. Copy the key — shown only once

---

## How the install flow works

1. Go to **Skills → Runway → Install** (or **Plugins → Credentials** for the target bot)
2. Paste your Runway API key
3. The install validates it against the Runway API
4. The key is stored in the bot's `auth-profiles.json`

The credential is stored under the `runway` provider key. Evolve never stores it centrally; it lives only on the bot's user account.

---

## Cost note

Runway charges per generation. The bot will use credits each time it generates video or images. Monitor spend via Usage → By Source. Set a spending cap in Cost Optimization if you want to limit Runway usage.

---

## Status values

| Status | What it means |
|--------|--------------|
| `missing_config` | No API key configured |
| `auth_failed` | Key rejected by Runway |
| `active` | Key verified — bot can use Runway models |

---

## Revoking access

1. In Runway: Account settings → API keys → revoke the key
2. In Evolve: Skills → Runway → Remove (or Plugins → Credentials → remove the Runway key)

---

## Related

- [upstream-plugin-skills-setup.md](upstream-plugin-skills-setup.md) — if Runway is installed as an upstream OpenClaw community skill
