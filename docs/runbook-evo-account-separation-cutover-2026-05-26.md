# Runbook — evo account separation cutover

Operator runbook for the Phase E migration described in
`docs/spec-evo-account-separation-2026-05-25.md`. Read this once before
running anything.

## What this runbook does

Migrates an existing Evolve install from the "evo runs on the privileged
`evolve` macOS account" shape to the "evo runs on the unprivileged
`evo` macOS account, admin daemon stays on `evolve`" shape.

After the runbook completes:

1. **`evo` user** owns evo's gateway, workspace, conversation history,
   and integration credentials.
2. **`evolve` user** still runs the admin daemon (LaunchDaemons under
   `ai.evolve.*` and `ai.openclaw.evolve.*`), still holds the
   admin-daemon API surface, still has cross-bot ACL read.
3. **Communication** between evo and the admin daemon happens over
   `/Users/Shared/evolve/admin-daemon.sock` with `getpeereid`-based
   auth. Evo loses its direct-fs reach to other bots; it talks to the
   admin daemon's typed API instead.
4. **Exec policy** for evo flips from `deny` (the old carve-out) to
   `full` (the new default for every member bot), since the original
   exec-approval-leak attack surface is now structurally closed by the
   account separation.

## Pre-flight (verify on the mini before starting)

All of these run read-only — no state change.

```
ssh pod-admin-user@mini

# 1. evolve user is currently primary
sudo cat /Users/Shared/evolve/network.json | python3 -c \
  'import json,sys; n=json.load(sys.stdin); print("primary:", n.get("primary")); print("user:", (n.get("bots") or {}).get(n.get("primary"), {}).get("user", "(unset = implicit primary_id)"))'
# Expected: primary: evolve (or evo)
#           user: evolve  (or unset, which defaults to primary_id)

# 2. evo gateway is currently loaded as the evolve user
sudo launchctl list ai.openclaw.evolve-gateway | head -5
sudo plutil -extract UserName raw /Library/LaunchDaemons/ai.openclaw.evolve-gateway.plist
# Expected: evolve

# 3. Phase E.2.a + E.3.* + E.4 code is deployed on the mini
which evolve-admin
sudo evolve-admin --help | grep -E "(provision-evo-account|migrate-evo-account-cutover)"
# Expected: both subcommands listed
```

If any of the above looks wrong, stop and investigate before proceeding.

## Step 1 — Provision the empty `evo` account (E.2.a)

If the `evo` macOS user doesn't exist yet on the mini, create it. This
is idempotent — re-running on an already-provisioned mini is a no-op.

```
sudo evolve-admin provision-evo-account
```

Verify:

```
dscl . list /Users UniqueID | grep -E "^evo\s"
sudo ls -la /Users/evo/.openclaw/
# Expected: empty .openclaw/ tree with workspace/, agents/, credentials/, logs/
```

## Step 2 — Dry-run the cutover (E.2.b)

This prints the migration plan but mutates nothing.

```
sudo evolve-admin migrate-evo-account-cutover
```

Review the plan. The output should look like:

```
  Phase E.2.b cutover — evo's gateway to the `evo` user (dry-run=True)
  Primary bot:        evolve
  Current macOS user: evolve
  Target macOS user:  evo
  Gateway plist:      /Library/LaunchDaemons/ai.openclaw.evolve-gateway.plist
  State source:       /Users/evolve/.openclaw/
  State destination:  /Users/evo/.openclaw/
  Excluded from copy: logs/, openclaw.json.clobbered.*, openclaw.json.bak, openclaw.json.last-good
  Steps:
    1. launchctl bootout system/ai.openclaw.evolve-gateway
    2. sudo cp -a each non-excluded child of /Users/evolve/.openclaw/ → /Users/evo/.openclaw/
    3. patch /Users/evo/.openclaw/openclaw.json agents.defaults.workspace → /Users/evo/.openclaw/workspace
    4. sudo chown -R evo:staff /Users/evo/.openclaw/
    5. set_evolve_read_acl('evo') — grant admin daemon ACL read
    6. network.json: bots.evolve.user = 'evo' (was 'evolve')
    7. rewrite gateway plist with UserName=evo + /Users/evo/ paths
    8. launchctl bootstrap system <plist>
    9. verify gateway responds on its port
  Dry-run — no changes applied. Re-run with --confirm to execute.
```

If the plan looks wrong (e.g. wrong primary_id, wrong current_user),
stop and reconcile network.json before proceeding.

## Step 3 — Execute the cutover

Operator-visible disruption: minutes-scale, gateway restart only.
During the cutover, the admin UI won't be able to reach evo's gateway
(which is the right shape — admin UI reaches evo via Telegram/admin
chat surface, neither of which holds long-running connections).

```
sudo evolve-admin migrate-evo-account-cutover --confirm
```

Watch the output. Each step should print a ✅:

```
  ✅ bootout ai.openclaw.evolve-gateway
  ✅ copied per-bot state /Users/evolve/.openclaw/ → /Users/evo/.openclaw/
  ✅ patched openclaw.json workspace path
  ✅ chown -R evo:staff /Users/evo/.openclaw
  ✅ set_evolve_read_acl('evo')
  ✅ network.json: bots.evolve.user = 'evo'
  ✅ rewrote /Library/LaunchDaemons/ai.openclaw.evolve-gateway.plist (UserName=evo, /Users/evo/ paths)
  ✅ bootstrap system <plist>
  ✅ gateway responding on port 19030
  ✅ Phase E.2.b cutover complete.
```

If a step fails, the migration halts. State is left at whatever step
halted; re-running picks up from there (each step is idempotent —
the precondition check sees what's already done and skips it).

If step 9 fails (gateway didn't respond within 30s), the state
mutations have all landed but the gateway didn't come up. Check:

```
sudo tail -50 /Users/evo/.openclaw/logs/gateway.err.log
sudo launchctl list ai.openclaw.evolve-gateway
sudo launchctl kickstart -k system/ai.openclaw.evolve-gateway
```

## Step 4 — Verify post-cutover behavior

```
# Gateway is up as the evo user
sudo launchctl list ai.openclaw.evolve-gateway | head -5
ps aux | grep -E "openclaw.*gateway" | grep -v grep
# Expected: evo as the process owner (not evolve)

# Network.json now records the user split
sudo cat /Users/Shared/evolve/network.json | python3 -c \
  'import json,sys; n=json.load(sys.stdin); pid=n.get("primary"); print("primary:", pid); print("user:", n["bots"][pid]["user"])'
# Expected: user: evo

# Evo can reach the admin daemon's unix socket
sudo ls -la /Users/Shared/evolve/admin-daemon.sock
# Expected: srw-rw---- evolve evolve 0 ... (mode 0660)

# Pick a registered evo tool that exercises the daemon and try it via
# Telegram or the admin-UI chat:
#   - "what bots are in the pod" (pod_state.bots — uses /api/status)
#   - "show team-bot-a's openclaw config" (config.bot — uses /api/admin/bot/.../openclaw-config)
#   - "trip the cost breaker on team-bot-b for 5 minutes" (action.bot.trip_breaker
#     — uses /api/admin/breakers/trip)
# Each should return success with `via: "admin_daemon"` in the response.

# Evo's exec policy is now full (post-Phase-E.4)
sudo cat /Users/evo/.openclaw/openclaw.json | python3 -c \
  'import json,sys; c=json.load(sys.stdin); print("exec.security:", c.get("tools",{}).get("exec",{}).get("security"))'
# Expected: exec.security: full
# (NOT deny — the Phase E.4 PR removed that carve-out)
```

## Rollback

If something is badly wrong, the source-side state still exists at
`/Users/evolve/.openclaw/` (the cutover only ADDED files to
`/Users/evo/.openclaw/` and modified network.json + the plist; it
didn't delete from `/Users/evolve/`). Rollback:

```
# Stop the new gateway
sudo launchctl bootout system/ai.openclaw.evolve-gateway

# Revert the plist UserName + paths manually (open in editor):
sudo $EDITOR /Library/LaunchDaemons/ai.openclaw.evolve-gateway.plist
# Replace `<string>evo</string>` under UserName with `<string>evolve</string>`
# Replace `/Users/evo/` paths with `/Users/evolve/`

# Revert network.json
sudo $EDITOR /Users/Shared/evolve/network.json
# Under bots.<primary>: change "user": "evo" back to "user": "evolve"

# Re-bootstrap
sudo launchctl bootstrap system /Library/LaunchDaemons/ai.openclaw.evolve-gateway.plist

# Verify the rollback
sudo launchctl list ai.openclaw.evolve-gateway | head -5
ps aux | grep -E "openclaw.*gateway" | grep -v grep
# Expected: evolve as the process owner
```

The /Users/evo/.openclaw/ tree can be left in place or removed —
it's harmless once the plist + network.json are back.

## What's next (Phase E.5, optional)

After the cutover settles, optionally clean up the residual bot state
at `/Users/evolve/.openclaw/`. Until then, it stays as a rollback
safety net.

```
# After confidence builds (a week of normal operation), remove
# the now-redundant bot state from the source side:
sudo rm -rf /Users/evolve/.openclaw/{agents,workspace,credentials,memory,identity,telegram,...}
```

This is Phase E.5 in the spec — purely cleanup, not load-bearing.

## Reference

- Spec: `docs/spec-evo-account-separation-2026-05-25.md`
- Phase E.1 audit: `docs/audit-evo-account-separation-2026-05-26.md`
- App-derived permissions parent spec: `docs/spec-app-derived-permissions-2026-05-24.md`
- Original /approve leak diagnosis: internal-only (the slash-command exfiltration channel that motivated the deny-on-evo carve-out)
