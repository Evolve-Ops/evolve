# Pre-Install Checklist

Work through this before running `evolve-admin setup --fresh`. The wizard will ask for most of these — having them ready saves time and prevents mid-install failures.

---

## Hardware

- [ ] Any Mac running macOS 14 (Sonoma) or later (16GB RAM minimum; 24GB recommended)
- [ ] Mac mini M4 is the recommended always-on box
- [ ] Linux host / Ubuntu 24.04 VPS — supported
- [ ] Admin account with sudo access
- [ ] Remote login enabled (System Settings → General → Sharing → Remote Login)
- [ ] Wired ethernet recommended for 24/7 reliability

---

## Software Prerequisites

```bash
# Check Python version (needs 3.9+)
python3 --version

# Install Node.js if needed (needs 20+)
brew install node
node --version

# Clone the repo
sudo git clone https://github.com/evolve-ops/evolve /Users/Shared/evolve-repo
```

---

## Accounts — Required

### LLM Provider (need at least one)

**Cloud providers:**

| Provider | Where to get a key | Notes |
|---|---|---|
| Anthropic | [console.anthropic.com](https://console.anthropic.com) → API Keys | Set a spend limit before installing |
| OpenAI | [platform.openai.com](https://platform.openai.com) → API Keys | GPT-4o, o1 |
| Google Gemini | [aistudio.google.com](https://aistudio.google.com) → API Keys | Gemini 2.0 Flash/Pro |
| xAI (Grok) | [console.x.ai](https://console.x.ai) → API Keys | Grok-2 |
| Mistral | [console.mistral.ai](https://console.mistral.ai) → API Keys | |

**Local / open-source (runs on the pod host itself):**

| Runner | Notes |
|---|---|
| [Ollama](https://ollama.com) | Easiest local setup. Good for tier3 background tasks. |
| [LM Studio](https://lmstudio.ai) | GUI-based, good for experimenting. |

A common setup: Ollama for tier3 analysis and background jobs, a cloud provider for tier2/tier1 user-facing work.

- [ ] At least one LLM API key (or Ollama installed)
- [ ] Spend limit set on any cloud provider account

### Messaging Channel (need at least one)

Each bot gets its own token. You can mix channels across bots.

| Channel | How to set up | Time |
|---|---|---|
| Telegram | Message [@BotFather](https://t.me/BotFather), run `/newbot` | 5 min |
| Slack | Create a Slack app at [api.slack.com](https://api.slack.com) | 15 min |
| Discord | Create a bot at [discord.com/developers](https://discord.com/developers) | 10 min |
| WhatsApp | Meta Business API — most complex | 20+ min |

For Telegram you'll also need your **Telegram user ID** (message [@userinfobot](https://t.me/userinfobot) to get it).

- [ ] At least one messaging channel token
- [ ] Your Telegram user ID (if using Telegram)

---

## Accounts — Strongly Recommended

### Brave Search API Key
Web search application for your bots.
- Sign up: [api.search.brave.com](https://api.search.brave.com)
- Free tier: 2,000 queries/month
- [ ] Brave Search API key

### Google OAuth Credentials
Unlocks Gmail, Calendar, Drive, Sheets, Docs tools for your bots.
- Go to [console.cloud.google.com](https://console.cloud.google.com)
- Create a project → Enable Gmail API, Calendar API, Drive API
- Create OAuth 2.0 credentials → Download as `credentials.json`
- This takes ~45 minutes the first time
- [ ] Google OAuth `credentials.json`

### Tailscale
Remote admin UI access from your laptop + MCP Bridge for Claude Desktop.
- Install: [tailscale.com](https://tailscale.com) — free for personal use
- Install on both the pod host and your laptop
- [ ] Tailscale installed on the pod host
- [ ] Tailscale installed on laptop (if you want remote admin access)

### Private GitHub Repository
Used for nightly security backups and drift detection. One repo per pod.
- Create a new **private** repo at [github.com/new](https://github.com/new)
- Name suggestion: `evolve-pod-backup` or similar
- [ ] Private GitHub repo URL noted: `https://github.com/___/___`

---

## Accounts — Optional (Add After First Bot is Running)

- [ ] **Dedicated security alert Telegram token** — separate from general notifications; adds alert independence
- [ ] **GitHub personal access token** — for coding bots (`repo`, `read:org` scopes)
- [ ] **ElevenLabs API key** — text-to-speech
- [ ] **Runway API key** — AI video generation
- [ ] **Perplexity API key** — alternative web search with citations
- [ ] **Home Assistant long-lived token** — smart home control
- [ ] **Slack bot token** — if using Slack as a channel
- [ ] **Discord bot token** — if using Discord as a channel

---

## Time Estimates

| Task | Time |
|---|---|
| LLM provider API key | 5 min |
| Telegram bot token + user ID | 5 min |
| Brave Search API key | 5 min |
| Tailscale setup | 15 min |
| GitHub private repo | 5 min |
| Google OAuth setup | ~45 min |
| **Minimal (one LLM + one channel, keys in hand)** | **~15 min** |
| **Full setup (all recommended accounts)** | **~90 min** |
| **Wizard run (all keys in hand)** | **~30 min** |

---

## Before You Run the Wizard

- [ ] All required keys collected and noted somewhere accessible
- [ ] Existing OpenClaw bot configs backed up
- [ ] Spend limits set on cloud LLM provider accounts
- [ ] You've read the [beta warnings](../README.md#before-you-install) in the README
- [ ] You have time to complete the install in one sitting (~30–90 min)

```bash
# When ready:
sudo evolve-admin setup --fresh
```
