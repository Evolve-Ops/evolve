# Setting Up Home Assistant

The Home Assistant skill gives your bot access to your smart home — lights, switches, sensors, climate, locks, and any other device managed by your Home Assistant instance. Commands flow from the bot through the Home Assistant REST API, staying on your local network.

**Install via:** Skills page → Home Assistant → Install

---

## What it does

After setup, the bot can:
- Control devices — turn lights on/off, set brightness, adjust thermostat, lock/unlock doors
- Query sensor state — current temperature, motion events, door/window open state
- Run automations and scripts defined in Home Assistant
- Announce text to media players (TTS)

---

## Prerequisites

- A running Home Assistant instance (local or accessible via Nabu Casa / Tailscale)
- Home Assistant 2023.1 or later
- A Long-Lived Access Token from your Home Assistant profile

---

## How to get a Long-Lived Access Token

1. In Home Assistant, click your profile (bottom-left)
2. Scroll to **Long-Lived Access Tokens**
3. Click **Create token**, give it a name (e.g., "Evolve bot"), copy the token immediately — it's only shown once
4. Also note your Home Assistant URL (e.g., `http://homeassistant.local:8123` or `https://your-instance.nabu.casa`)

---

## How the install flow works

1. Go to **Skills → Home Assistant → Install**
2. Enter your Home Assistant URL (the base URL without `/api/`)
3. Paste your Long-Lived Access Token
4. The install flow calls the HA REST API at `/api/` to verify the token and URL are valid
5. On success, the token and URL are stored in the bot's credential store

The credential lives in `auth-profiles.json` under the `home_assistant` provider key. It is never stored centrally — it stays on the bot's user account.

---

## Network access

If your Home Assistant runs on your local network:
- The bot (running on the Mac mini) must be able to reach `homeassistant.local:8123` or whatever your HA URL is
- No firewall rules needed if the Mac mini and HA are on the same network

If your Home Assistant uses Nabu Casa or another remote tunnel:
- Use the HTTPS URL Nabu Casa provides
- The long-lived access token works the same way

---

## Status values

| Status | What it means |
|--------|--------------|
| `missing_config` | No URL or token configured |
| `connection_failed` | HA URL not reachable from the Mac mini |
| `auth_failed` | Token rejected by Home Assistant |
| `active` | Token and URL verified — bot can control devices |

---

## Revoking access

1. In Home Assistant, go to your profile → Long-Lived Access Tokens → delete the token named "Evolve bot"
2. In Evolve: Skills → Home Assistant → the bot's card → **Remove** to clear the stored credential

---

## Related

- [apple-local-setup.md](apple-local-setup.md) — for macOS-native automation (Shortcuts, HomeKit directly)
