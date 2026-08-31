---
title: "Evolve Pods — the multi-pod desktop app"
slug: pods-app
audience: public
last_reviewed: 2026-06-23
concepts:
  - pods-app
  - desktop-app
  - multi-pod
ui_surface: null
related_specs:
  - internal/spec-multipod-desktop-distribution-2026-06-19.md
---

# Evolve Pods — the multi-pod desktop app

**Evolve Pods** is a small macOS desktop app that puts **one Dock icon** over all
your pods and lets you **tab between them**. Each pod opens in its own window
pointed at that pod's dashboard, keeps its own login, and stays an independent
island — the app adds no shared state and no bridge between pods. It's pure
navigation.

If you run a **single pod**, you don't need this app at all — just use the
dashboard in your browser, or [install it as a PWA](pwa-install.md). Evolve Pods
earns its keep only when you're juggling several pods and want one window for all
of them.

---

## Current status: build it yourself (for now)

Be aware of where this app stands today, honestly:

> 🚧 **Evolve Pods is operator-built, not yet distributed.** There is no signed
> download. You build it once on your Mac from the source in the repository. A
> signed, auto-updating download is **specified but deferred** — it will be built
> when the app needs to leave the machine that built it, not before. Until then,
> the local build below is the supported path.

That means today you get the app by compiling it yourself. It's a short, one-time
build.

---

## Build and install it

The app is built with **Tauri v2**. You need **Rust** and the **Tauri CLI** — you
do *not* need Node (the interface is plain static files served by Tauri).

```bash
# One-time toolchain setup
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
cargo install tauri-cli --version "^2.0" --locked

# Build the app
cd desktop/src-tauri
cargo tauri build
```

This produces `Evolve Pods.app` (and a `.dmg`) under
`target/release/bundle/macos/`. **Drag `Evolve Pods.app` into your Applications
folder** and you're done.

> ⚠️ **Gatekeeper note — the app is unsigned.** Because there's no Apple
> code-signing certificate yet, macOS will refuse to open it on a double-click the
> first time. **Right-click the app → Open**, then confirm in the dialog. macOS
> remembers your choice, so you only do this once.

---

## Using it

- **One window, one icon** for all your pods.
- **Add a pod** from the settings panel by name + dashboard URL; the app can also
  auto-discover pods on your machine (`localhost`) and across your Tailscale
  network so you don't have to type the URL.
- **Tab between pods** — the tab bar appears once you have more than one pod, and
  you can switch with the Pods menu, **Ctrl+Tab**, or **⌘1–9**.
- **Offline-tolerant** — the app always opens even if a pod is down; a dead pod
  shows a "Can't reach…" message with a Retry button and never blocks the others.
- Each pod still shows its **own pairing screen** on first visit — that's correct;
  the app holds no tokens and bridges nothing between pods.

---

## What "deferred" means for updates

You **don't** rebuild this app for ordinary Evolve updates. The app loads each
pod's dashboard as live remote content, so when a pod updates itself, its
dashboard updates and the app shows the new version on the next load — exactly
like a browser. You only rebuild Evolve Pods if the **app's own** code changes,
which is rare.

When signed distribution and a built-in auto-updater land (the deferred
milestone), this manual build-and-drag path retires. Until then, it's the whole
story.
