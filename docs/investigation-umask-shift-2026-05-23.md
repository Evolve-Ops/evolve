# Investigation: shared-dir file-mode shift to 600 — root cause + remaining exposure

Date: 2026-05-24
Author: Claude (investigation only; no code changes)

## TL;DR

The umask shift from `022` → `077` for files written by bot processes on the
mini is **NOT an upstream OpenClaw or Node change.** It comes from **our own
gateway-LaunchDaemon plist templates**: both
`packages/admin/evolve_admin/deploy.py:3232` and
`packages/admin/evolve_admin/setup_wizard.py:763` write

```xml
<key>Umask</key>
<integer>63</integer>
```

where `63 == 0o77`. That key has lived in the templates since 2026-04-14 /
2026-04-18, but only took effect on the mini between 2026-05-21 18:24–18:28
when something redeployed and rewrote all seven gateway plists (team-bot-a, team-bot-c,
admin-bot, security-bot, team-bot-b, personal-bot, evolve).

PR [#1520](https://github.com/evolve-ops/evolve/pull/1520) already mitigates the
two highest-traffic paths (`{shared}/annotations/<bot>/`,
`{shared}/<bot>/turns/`) via inheritable `evolve allow read,...` ACLs. **One
load-bearing path is still broken**: `{shared}/metrics/<bot>/recent-transcripts.json`
is mode 600 and not readable by `evolve`. Two evolve-user consumers
(`app_posture_reflect.py`, `generator_runner.py`) read it as evolve — both are
silently failing or about to fail on every bot whose buffer was rewritten after
the gateway restart.

The flat `{shared}/metrics/<bot_id>-<date>.json` written by `MetricsCollector.ts`
also flipped to 600 but **has no Python consumer** — looks like dead-write
output.

The umask covers every `fs.*` write path the gateway-loaded plugin makes, not
just `appendFileSync`. Verified call sites below.

## Live evidence

### Gateway daemons all carry Umask=63

```
$ sudo /bin/launchctl print system/ai.openclaw.team-bot-a-gateway | grep umask
    umask = 77
```

Same for team-bot-c, admin-bot, security-bot, team-bot-b, personal-bot, evolve. Plist source on disk:

```
$ sudo grep -E '<integer>(63|18)</integer>' /Library/LaunchDaemons/ai.openclaw.team-bot-a-gateway.plist
    <integer>63</integer>
```

### Plist mtimes (rewritten same minute, May 21 18:24–18:28)

```
-rw-r--r--+ root wheel May 21 18:24 ai.openclaw.team-bot-a-gateway.plist
-rw-r--r--+ root wheel May 21 18:25 ai.openclaw.team-bot-c-gateway.plist
-rw-r--r--  root wheel May 21 18:25 ai.openclaw.personal-bot-gateway.plist
-rw-r--r--+ root wheel May 21 18:26 ai.openclaw.admin-bot-gateway.plist
-rw-r--r--+ root wheel May 21 18:27 ai.openclaw.security-bot-gateway.plist
-rw-r--r--  root wheel May 21 18:28 ai.openclaw.evolve-gateway.plist
-rw-r--r--+ root wheel May 21 18:28 ai.openclaw.team-bot-b-gateway.plist
```

### Git provenance of the `Umask=63` lines

```
b753b8f5  2026-04-18  fix(deploy): install gateway LaunchDaemon + deploy_bot in _full_deploy
          adds <integer>63</integer> to install_bot_gateway_plist (deploy.py)
58482c5c  2026-04-14  fix(setup): generate gateway plist running node directly...
          adds <integer>63</integer> to evolve-gateway plist in setup_wizard.py
```

Neither commit message explains *why* `0o077` was chosen. The likely reason
was a defensive default (deny group/world by default for daemon-written
files), but it landed without an audit of which downstream consumers depend
on the resulting file being mode 644.

### Versions on the mini (ruled out as cause)

- Node: `v22.22.3`, symlinked May 21 00:30 (Cellar/node@22). Per-bot
  `sudo -u <bot> node --version` agrees for every bot. Node was downgraded
  from 25 → 22 some time before the umask shift, so it predates the boundary
  by ~18 hours and predates the actual file-mode flip (May 21→22 boundary for
  metrics, May 22→23 for annotations).
- OpenClaw: `2026.5.22 (a374c3a)`, installed May 23 20:36
  (`/opt/homebrew/lib/node_modules/openclaw/package.json` and the
  `openclaw.mjs` symlink). The CHANGELOG for 5.22 contains no `umask`-related
  entry. A full grep of `/opt/homebrew/lib/node_modules/openclaw/dist/**`
  found exactly two `process.umask()` calls, both in
  `json-files-C2hqjU--.js` and both for *dirMode default computation*, not
  for setting the process umask. OC does not set umask anywhere.

So upstream is exonerated. The shift is entirely from our plist templates
combined with the May 21 redeployment that rewrote the live plists.

## Affected paths (live mini state, 2026-05-24)

Columns: path, current mode of files newly created post-flip, owner pattern,
whether the parent dir carries an inheritable `evolve` ACL (per
`set_evolve_read_acl` or PR #1520), whether evolve can actually read.

| Path | Mode (post-flip) | Owner | Inheritable ACL? | Evolve reads? | Status |
|---|---|---|---|---|---|
| `{shared}/annotations/<bot>/<date>.jsonl` | 600 | bot | yes (PR #1520) | YES | **fixed** |
| `{shared}/annotations/<bot>/cost_events-<date>.jsonl` | 644 | bot | yes (PR #1520) | YES | unaffected (cost-converter daemon, no `Umask=63`) |
| `{shared}/<bot>/turns/turns-<date>.jsonl` | 600 | bot | yes (PR #1520) | YES | **fixed** |
| `{shared}/metrics/<bot>/recent-transcripts.json` | 600 | bot | **NO** | **NO** | **BROKEN — read by evolve** |
| `{shared}/metrics/<bot>-<date>.json` (flat) | 600 | bot | **NO** | **NO** | dead-write (no Python consumer found) |
| `{shared}/metrics/<bot>/cost-<date>.json` | 644 | evolve | n/a | n/a | written by evolve cron, unaffected |
| `{shared}/feedback/rejections.jsonl` | 644 | evolve | n/a | YES | written by evolve-gateway plugin against the evolve umask, currently 644 |
| `/Users/<bot>/.openclaw/workspace/evolve/manifest-reflex-queue.jsonl` | (n/a — no live file) | bot | yes (set_evolve_read_acl) | YES (would be) | safe via existing ACL |
| `/Users/<bot>/.openclaw/workspace/evolve/defer-queue.jsonl` | (n/a — no live file) | bot | yes (set_evolve_read_acl) | YES (would be) | safe via existing ACL |
| `/Users/<bot>/.openclaw/workspace/evolve/audits/.../trail.jsonl` | 644 (pre-existing) | bot | yes | YES | mode survives because file pre-existed; new files would inherit ACL |
| `/Users/<bot>/.openclaw/workspace/evolve/audit_outbox/<date>/rec-*.json` | 600 (always) | bot | yes | YES | already 600 before flip; ACL covers it |

### Live read-access probe results

```
$ sudo -u evolve test -r /Users/Shared/evolve/metrics/team-bot-a/recent-transcripts.json   → NO
$ sudo -u evolve test -r /Users/Shared/evolve/metrics/team-bot-a-2026-05-23.json          → NO
$ sudo -u evolve test -r /Users/Shared/evolve/team-bot-a/turns/turns-2026-05-23.jsonl     → YES
$ sudo -u evolve test -r /Users/Shared/evolve/annotations/team-bot-a/2026-05-23.jsonl     → YES
```

### Dir ACL state (the load-bearing column)

```
$ ls -laeO /Users/Shared/evolve/metrics/team-bot-a/ | head
drwxrwxrwt 24 team-bot-a wheel - 768 May 24 12:23 .
 0: user:team-bot-a allow list,add_file,search,add_subdirectory,delete_child,readattr,...
```

One ACE for `user:team-bot-a`, no `file_inherit` flag, **no `evolve` ACE.** Compare:

```
$ ls -laeO /Users/Shared/evolve/team-bot-a/turns/
 0: user:evolve allow list,search,readattr,file_inherit,directory_inherit
 1: user:evolve allow list,search,readattr
```

Plus inherited files like `turns-2026-05-23.jsonl`:

```
$ ls -leO /Users/Shared/evolve/team-bot-a/turns/turns-2026-05-23.jsonl
-rw-------+ 1 team-bot-a wheel - 9052 May 23 14:09
 0: user:evolve inherited allow read,execute,readattr
```

This is what PR #1520 does for annotations/turns and what `metrics/<bot>/`
needs.

## Consumers of the broken path

`{shared}/metrics/<bot>/recent-transcripts.json` is read by:

- [packages/analyzer/app_posture_reflect.py:209](packages/analyzer/app_posture_reflect.py:209)
  — `app-posture-review` daemon (`ai.openclaw.evolve.app-posture-review.plist`)
  runs as evolve and embeds the buffer into per-app posture proposals.
- [packages/analyzer/generator_runner.py:253](packages/analyzer/generator_runner.py:253)
  — generator orchestration (evolve user) feeds the buffer to evidence-gathering
  generators.
- [packages/admin/evolve_admin/pod_state/turns.py:62](packages/admin/evolve_admin/pod_state/turns.py:62)
  — admin server (evolve user) surfaces the buffer in primary-state routes
  (recent-messages dashboard / Dispatcher Health).

All three currently fail on bots whose buffer was rewritten after the
gateway restart, which is every bot (team-bot-a, team-bot-c, admin-bot, security-bot, team-bot-b, personal-bot,
evolve). The failure mode is `PermissionError`/`EACCES` on read — which
matters for `app_posture_reflect` and `generator_runner` because they
silently drop the buffer (returns empty / proceeds without context); the
`/api/primary-state` route surfaces a server-side error.

## Surface beyond `appendFileSync`

The launchd `Umask` applies to every file-creation syscall in any child the
gateway spawns. Plugin call sites that create files in the gateway umask
environment:

```
packages/plugin/src/observer/TurnObserver.ts:1624   fs.promises.writeFile(tmpFile, …)         turns staging
packages/plugin/src/observer/TurnObserver.ts:2590   fs.appendFileSync(filePath, …, { mode: 0o644 })   turns-<date>.jsonl
packages/plugin/src/observer/TurnObserver.ts:2631   fs.appendFileSync(filePath, …)            annotations <date>.jsonl
packages/plugin/src/observer/RecentTranscriptCapture.ts:103   fs.writeFileSync(tmpPath, …)    recent-transcripts.json staging
packages/plugin/src/collector/MetricsCollector.ts:91   fs.writeFileSync(tmpPath, …)           metrics/<bot>-<date>.json staging
packages/plugin/src/tools/RecordApplicationTool.ts:269   fs.appendFileSync(file, …, { flag: "a" })   manifest-reflex-queue.jsonl
packages/plugin/src/tools/DeferTool.ts:207           fs.appendFileSync(file, …, { flag: "a" })       defer-queue.jsonl
packages/plugin/src/api/networkRoutes.ts:57          fs.writeFileSync(tmp, …)                  network.json staging (via writeJSON)
packages/plugin/src/api/networkRoutes.ts:227         fs.appendFileSync(rejectionsPath, …)      feedback/rejections.jsonl
```

So the surface is `writeFileSync`, `promises.writeFile`, and `appendFileSync`
— not just appends. None of them passes an explicit mode that *defeats* the
umask (the `{ mode: 0o644 }` at TurnObserver.ts:2590 is masked by umask
before becoming the file mode; that's how POSIX `open(2)` works).

A note on the `mode: 0o644` argument: it's harmless on macOS-with-umask-077
because `0o644 & ~0o077 = 0o600`. Anyone who reads that line and thinks the
file will be 644 is mistaken; if we wanted that, we'd need an explicit
`fs.chmodSync` after creation (which would not solve the underlying
visibility-to-evolve problem any better than fixing the dir ACL).

## Why some files look unaffected

Two patterns explain why some `bot`-owned files in the shared dir are still
mode 644 today:

1. **The file pre-existed the gateway restart.** `appendFileSync` on an
   existing file does not change mode. e.g.
   `/Users/team-bot-a/.openclaw/workspace/evolve/audits/journal/trail.jsonl` is 644
   because it was created at 644 in April and has been appended to ever
   since.
2. **The writer isn't a gateway child.** `cost_event_converter.py`,
   `audit-runner.py`, `tier3-runner`, the `apply.<bot>` daemon all have
   their own LaunchDaemons (`ai.openclaw.evolve.cost-converter.*`,
   `ai.openclaw.evolve.audit-runner.*`, etc.) and **none of those plists has
   a `Umask` key.** So they spawn with the launchd default umask (effectively
   022) and produce 644 files. That's why
   `{shared}/annotations/<bot>/cost_events-<date>.jsonl` is 644 (cost-converter)
   but `{shared}/annotations/<bot>/<date>.jsonl` is 600 (TurnObserver from a
   gateway-spawned OC session).

The evolve-user files in `{shared}/annotations/evolve/<date>.jsonl` are 644
even post-flip. Empirically, evolve-gateway-spawned OC sessions appear to
produce 644 files despite the plist saying `Umask=63`. I could not fully
explain this from launchd documentation, and the team-bot-a/team-bot-c/etc. cases
unambiguously demonstrate Umask=63 *does* propagate from the gateway plist
in practice; whatever causes evolve's branch to behave differently doesn't
change the conclusion that *some* gateway-spawned writes land at 600.

## Recommended follow-up patches

The PR #1520 pattern is the simplest fix: pre-create the dir with the
`evolve allow read,readattr,list,search,file_inherit,directory_inherit` ACE
so any file created there inherits an evolve-read grant regardless of mode.
That mirrors the existing pattern at
[packages/admin/evolve_admin/deploy.py:2528-2533](packages/admin/evolve_admin/deploy.py:2528).

### Patch 1 — `{shared}/metrics/<bot>/` (CRITICAL, ship soon)

Three evolve-user consumers depend on `recent-transcripts.json` in this dir.
Currently broken on every bot.

Apply at [packages/admin/evolve_admin/deploy.py:2553-2555](packages/admin/evolve_admin/deploy.py:2553)
(the existing `_create_bot_subdir(shared_dir / "metrics" / bot_id, 0o1777)`
call). Change to include the same ACL argument as `turns/`:

```python
_create_bot_subdir(
    shared_dir / "metrics" / bot_id,
    0o1777,
    acl="evolve allow read,readattr,list,search,file_inherit,directory_inherit",
)
```

Then run `evolve-admin repair-acls` (or whatever the field tool is) once
to apply retroactively to the seven existing `metrics/<bot>/` dirs and to
the live `recent-transcripts.json` files inside them. The dir ACL alone
won't help files already-on-disk — those need a one-shot `chmod +a`.

### Patch 2 — `{shared}/metrics/` (the flat-file parent)

`MetricsCollector.ts` writes `metrics/<bot_id>-<date>.json` directly under
`{shared}/metrics/` (not under `metrics/<bot_id>/`). These are currently 600
and evolve cannot read them. Two options:

- **a)** Add `evolve allow read,…,file_inherit` on `{shared}/metrics/`
  itself. This will apply to every per-day-dir under it too (`metrics/<date>/`
  date-bucketed dirs), which is harmless because those are already evolve-owned.
- **b)** Decide the flat file is dead-write and remove the
  `MetricsCollector.flush()` writer. I searched
  packages/{analyzer,admin}/ for `metrics/[^/]*-2026-` and similar patterns
  and found no Python consumer; the only places `metrics/<bot_id>-<date>.json`
  is mentioned are the plugin source itself and a deploy.py comment about
  the dir being multi-writer.

Recommend (b) if confirmed unused — fewer load-bearing surfaces means fewer
ACL edge cases. Worth grepping more carefully (or just removing it behind a
short revert window) before deleting. Recommend (a) as a belt-and-suspenders
fallback if you keep the writer.

### Patch 3 — preventative — drop `<key>Umask</key>` from the gateway plists

The deeper fix is to remove the `Umask=63` line from both
[packages/admin/evolve_admin/deploy.py:3232](packages/admin/evolve_admin/deploy.py:3232)
and [packages/admin/evolve_admin/setup_wizard.py:763](packages/admin/evolve_admin/setup_wizard.py:763)
so gateway-spawned writes go back to umask 022 / mode 644. Trade-off: the
`Umask=63` was presumably set deliberately for some defensive reason (deny
group/world by default). If we drop it, we should:

1. Confirm no current sensitive file relies on it (most candidate paths are
   already 644 because their writers don't pass through the gateway).
2. Keep the inheritable evolve-read ACLs on the relevant dirs anyway — they
   are the correct mechanism (ACL grants the right user, mode bits are too
   blunt).

Personally I'd vote: do Patch 1 immediately (critical), do Patch 3 as a
follow-on once we audit who actually wanted the 077 default, and leave Patch
2 for after deciding the flat-metrics writer's fate.

### Patch 4 — preventative — audit other launchd plists for missing/inconsistent `Umask`

The seven gateway plists carry `Umask=63`. None of the
audit-runner / cost-converter / apply / test / measure / report / outcome
plists carry any `Umask` key. That inconsistency means our umask story is
implicit and varies per daemon. Worth documenting in CLAUDE.md (alongside
the existing "writes go via /tmp + sudo /bin/cp" note) once the immediate
breakage is resolved.

## Things I did *not* do

- Did not apply any code changes (investigation-only per task instructions).
- Did not file an upstream OC issue. There is no upstream issue here.
- Did not exhaustively grep the dist-bundled OC for unusual umask handling
  past confirming the two `process.umask()` reads are dirMode-default-only.
  If a future symptom suggests OC is doing something exotic, that grep
  should be re-run against the then-current OC version, not 2026.5.22.
