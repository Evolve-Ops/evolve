# Contributing to Evolve

Evolve is in alpha. The most valuable thing you can do right now is install it, use it, break it, and tell us what happened.

This document explains how to do that usefully.

---

## What We Need Most

In rough priority order:

1. **Install reports** — did the setup wizard work? Where did it fail?
2. **Bug reports** — something broke, behaved unexpectedly, or produced wrong output
3. **Documentation gaps** — something was unclear, missing, or wrong in the docs
4. **Environment reports** — hardware or OS variants that aren't M4/macOS14

We are not yet actively soliciting feature requests, but the issue template is there if you have something specific.

---

## Filing a Good Bug Report

Use the [bug report template](https://github.com/evolve-ops/evolve/issues/new?template=bug_report.md).

A useful bug report has:

- **What you did** — the exact command or UI action
- **What you expected** — what should have happened
- **What actually happened** — exact error message, log output, or behavior
- **Your environment** — Mac model, macOS version, Evolve version (`evolve-admin --version`), number of bots
- **Relevant logs** — see below for where to find them

### Log locations

```
/Users/Shared/evolve/logs/          # Evolve analysis and proposal logs
/Users/Shared/evolve/incidents/     # Security and drift incidents
~/Library/Logs/evolve-admin.log     # Admin server log
~/Library/Logs/heal.log             # Gateway health monitor log
~/Library/Logs/audit.log            # Security audit log
```

Paste the relevant section of the log directly into the issue. Don't attach log files unless they're very large.

---

## What "Beta Tester" Means Here

You're in this group because you can tell the difference between something broken and something wrong, and you'll say so. Specifically:

- You're comfortable in Terminal and with macOS system accounts
- You understand that Evolve touches launchd, shared directories, and OpenClaw config files
- You've backed up your existing bot configs before installing
- You have a machine you can afford to wipe if something goes seriously wrong
- You'll file an issue rather than quietly working around a bug

If something breaks your existing setup, that is **important signal** and we want to hear about it — it's not a reason to stay quiet.

---

## Before You File an Issue

1. Check [existing issues](https://github.com/evolve-ops/evolve/issues) — it may already be reported
2. Check [discussions](https://github.com/evolve-ops/evolve/discussions) — it may be a known limitation
3. Run `evolve-admin status` and include the output
4. Note which pillar or component is involved (Administration, Monitoring, Security, etc.)

---

## Code Contributions

We're not yet set up for external code contributions in a structured way. If you find a bug and have a fix, open an issue first and describe the fix — we'll coordinate from there. Don't open PRs without a corresponding issue.

---

## Discussions

[GitHub Discussions](https://github.com/evolve-ops/evolve/discussions) is the right place for:

- "Is this expected behavior?"
- "How do I configure X?"
- "Has anyone else seen this?"
- General feedback that isn't a specific bug

---

## What We Won't Act On (Right Now)

- Requests to support pod hosts beyond macOS and Linux (Ubuntu)
- Multi-machine pod architecture
- Delegated comms mode (explicitly not supported — see product-vision.md)
- Windows pod-host support (Linux / Ubuntu is supported)

These may happen eventually. They're not on the near-term roadmap.

---

Thank you for being a alpha tester. Your signal is what makes this better.
