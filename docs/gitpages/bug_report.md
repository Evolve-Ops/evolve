---
name: Bug Report
about: Something broke, behaved unexpectedly, or produced wrong output
title: "[BUG] "
labels: bug
assignees: ''
---

## What happened

<!-- What did you observe? Include exact error messages, unexpected behavior, or wrong output. -->

## What you expected

<!-- What should have happened? -->

## Steps to reproduce

<!-- How do we reproduce this? Be as specific as possible. -->

1. 
2. 
3. 

## Environment

- **Host:** (e.g. Mac mini M4, MacBook Pro M3, Ubuntu 24.04 VPS)
- **OS version:** (e.g. macOS 14.4, Ubuntu 24.04)
- **Evolve version:** (run `evolve-admin --version`)
- **Number of bots in pod:**
- **Install method:** (fresh install / upgrade from version X)

## Which component is involved?

<!-- Check all that apply -->

- [ ] Setup wizard (`evolve-admin setup --fresh`)
- [ ] Bot deploy (`evolve-admin deploy`)
- [ ] Admin dashboard (localhost:5050)
- [ ] Monitoring / health score
- [ ] Cost management
- [ ] Security / audit
- [ ] Better Engine / proposals
- [ ] Continuity Engine
- [ ] Applications / App Gallery
- [ ] Forge
- [ ] MCP Bridge / Claude Desktop integration
- [ ] Other: ___

## Relevant logs

<!-- 
Log locations:
  /Users/Shared/evolve/logs/         (analysis, proposals)
  /Users/Shared/evolve/incidents/    (security, drift)
  ~/Library/Logs/evolve-admin.log    (admin server)
  ~/Library/Logs/heal.log            (gateway monitor)
  ~/Library/Logs/audit.log           (security audit)

Paste the relevant section here. Trim to what's relevant.
-->

```
paste log output here
```

## Additional context

<!-- Anything else that might be relevant — recent changes, unusual config, etc. -->
