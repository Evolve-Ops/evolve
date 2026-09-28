---
title: "Install the Evolve app on your phone or desktop"
slug: pwa-install
audience: public
last_reviewed: 2026-06-23
concepts:
  - pwa
  - add-to-home-screen
  - mobile-install
  - https
  - tailscale
ui_surface: null
related_specs: []
---

# Install the Evolve app on your phone or desktop

The Evolve admin dashboard is a Progressive Web App (PWA) — you can install it as
a real app with its own icon, on a phone or on a desktop, straight from the
browser. No app store, no separate download.

---

## Before you start: HTTPS is required for phone install

Browsers only offer the "install this app" option on a page served over **HTTPS**
(or over `localhost`). On a phone you're not on localhost, so you need HTTPS
first.

The simplest way to get there is **Tailscale**, which gives your pod a private
`https://…ts.net` address reachable only by your own devices — no domain name, no
public exposure. Once Tailscale is running on the pod, enable HTTPS with one
command:

```bash
sudo evolve-admin enable-https
```

Your dashboard URL changes from `http://your-host:5050` to
`https://your-host.your-tailnet.ts.net`. Same dashboard, now installable on a
phone. (If you run the dashboard LAN-only over plain HTTP, it still works in a
browser — you just can't install it on a phone. That's a fine tradeoff for a
single-laptop setup.)

Make sure Tailscale is also installed and signed in on the phone you want to
install the app on, so it can reach that address.

---

## On iPhone or iPad (Safari)

1. Open **Safari** and go to your pod's HTTPS URL.
2. Tap the **Share** button (the square with an arrow).
3. Tap **Add to Home Screen**.
4. Confirm — the Evolve app appears on your home screen like any other app.

> Add to Home Screen only appears in **Safari** on iOS, not in Chrome or other
> browsers.

---

## On Android (Chrome)

1. Open **Chrome** and go to your pod's HTTPS URL.
2. Tap the **install banner** that appears at the bottom, **or** open the Chrome
   menu (⋮) and tap **Install app** / **Add to Home screen**.
3. The Evolve app installs and appears in your app drawer.

---

## On desktop (Chrome or Edge)

You can install the dashboard as a desktop app too — handy for a dedicated
window with its own Dock/taskbar icon:

1. Open **Chrome** or **Edge** and go to your dashboard URL.
2. Click the **install icon** in the right-hand side of the address bar (a small
   monitor/▾ icon), **or** open the browser menu and choose **Install Evolve…** /
   **Apps → Install this site as an app**.
3. The dashboard opens in its own window and gets an icon you can pin.

Desktop install works over `http://localhost:5050` as well as over HTTPS, so you
don't strictly need Tailscale for the desktop app — though HTTPS is still
recommended, and it's required if you also want push notifications for alerts.

---

## Why install it?

- **One tap** to your pod instead of typing a URL.
- A **standalone window** with no browser chrome.
- On a phone over HTTPS, the foundation for **push notifications** when an alert
  fires.

To set up the underlying HTTPS access this builds on, see your platform guide:
[macOS](install-macos.md) or [Linux/VPS](install-linux-vsp.md).
