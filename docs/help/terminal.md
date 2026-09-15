---
title: "Help: Terminal Page"
slug: terminal
audience: public
last_reviewed: 2026-06-23
concepts:
  - terminal
ui_surface: admin.terminal
related_specs: []
---

# Help: Terminal Page

The Terminal page (in the **Operate** bucket) is a real shell on your pod,
embedded right in the admin UI. It's there for the moments when you'd otherwise
SSH into your mini — checking a file, restarting a service, running a quick
command to see what's going on — without leaving the browser.

A few things to know:

- **It runs on your pod, not your laptop.** Commands execute on the pod machine that
  hosts your bots. For commands meant for your own machine, use your laptop's
  terminal app instead.
- **It's hands-on-keyboard, for you.** This is an operator tool you drive
  yourself. Your bots don't use it, and nothing types into it on your behalf.
- **It can change things.** A shell on your pod can read pod state, restart
  services, and make system-level changes. Treat it with the same care you'd
  give an SSH session — the first time you open it, you'll see a one-time
  heads-up confirming you understand that.

The header shows which pod you're connected to and whether the session is idle
or active. If the connection drops, a **Reconnect** button brings it back.

If you're not comfortable at a command line, you rarely need this page — the
rest of the admin UI and the chat assistant cover day-to-day operation. The
Terminal is the escape hatch for when you want to look under the hood directly.
