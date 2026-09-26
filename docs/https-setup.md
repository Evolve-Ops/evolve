# Setting up HTTPS for your Evolve pod

## Why

You need HTTPS to:

- Install the admin UI as an app on your phone (Add to Home Screen)
- Receive push notifications for alerts

Without HTTPS the web UI still works from a laptop, but the install path on your phone doesn't.

## What you need

- The Evolve mini running and reachable on your tailnet today
- Tailscale installed and signed in (you already have this — it's how you reach the admin UI from your laptop)
- A one-time toggle in your Tailscale admin console (instructions below)

## One-time Tailscale setup

The first time anyone in your tailnet wants HTTPS, you have to enable HTTPS certificate provisioning. This is a Tailscale account-level setting, not a mini-level one — you only do it once per tailnet, not once per pod.

1. Open [https://login.tailscale.com/admin/dns](https://login.tailscale.com/admin/dns) in your browser.
2. Scroll down to the **HTTPS Certificates** section.
3. Click **Enable HTTPS**.

This is free.

## Enable HTTPS on your pod

SSH to your mini and run:

    sudo evolve-admin enable-https

This will:

- Verify your Tailscale setup
- Set up Tailscale to handle HTTPS on your pod's URL
- Update Evolve's config to use the HTTPS URL
- Verify the new URL works

Takes about 30 seconds.

## What you'll see

Before:

    http://your-mini-host:5050

After:

    https://your-mini-host.your-tailnet.ts.net

Same admin UI, just over HTTPS. Reachable only from devices on your tailnet — your laptop, plus your phone once Tailscale is installed there. Not exposed to the public internet.

## Installing the admin UI on your phone

After HTTPS is enabled:

### iPhone or iPad

1. Open Safari and go to your pod's HTTPS URL.
2. Tap the share button.
3. Tap **Add to Home Screen**.
4. The app appears on your home screen.

### Android

1. Open Chrome and go to your pod's HTTPS URL.
2. Tap the install banner that appears, or open the Chrome menu and tap **Install**.

## Disable or rollback

If something goes wrong, or you want to go back to HTTP:

    sudo evolve-admin disable-https

This reverses everything `enable-https` did. Idempotent — safe to run more than once.

## Troubleshooting

- **"Tailscale not installed"** — Install it from [https://tailscale.com/download/mac](https://tailscale.com/download/mac), then re-run.

- **"Tailscale CLI not on PATH"** — If you installed Tailscale via the Mac App Store, the CLI lives at `/Applications/Tailscale.app/Contents/MacOS/Tailscale` and isn't on your shell's PATH by default. Either re-run `enable-https` (it auto-finds the App Store path) or symlink it once:

        sudo ln -s /Applications/Tailscale.app/Contents/MacOS/Tailscale /usr/local/bin/tailscale

- **"HTTPS provisioning not enabled in your tailnet"** — Do the [One-time Tailscale setup](#one-time-tailscale-setup) above, then re-run.

- **Cert errors in your browser** — It can take a minute or two for Tailscale to issue the first certificate. Wait 30 seconds and reload.

- **Banner still shows after enabling HTTPS** — The admin UI re-checks every 60 seconds. Refresh the page once to pick it up immediately.

## Pods not on Tailscale

If you've chosen not to run Tailscale (e.g. LAN-only access from a single laptop), you can still use the admin UI over plain HTTP — you just can't install the PWA on a phone. That's an acceptable v1 tradeoff. Caddy and self-signed certificates are options for self-hosters who want to leave Tailscale entirely, but they aren't wired into the wizard today.
