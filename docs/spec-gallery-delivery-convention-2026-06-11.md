# Gallery delivery convention — post-OpenClaw-2026.6 (P0 migration)

**Date:** 2026-06-11 · **Status:** approved (P0 execution)
**Motivating incident:** every scheduled user-facing send pod-wide dead since Jun 3
(OpenClaw 2026.6.1 removed `POST /api/message`), masked as `unmeasurable` in the
delivery-monitor ledger. Found by the M4/U1 activation proof run
([decision doc](decision-add-bot-m4-u1-proof-2026-06-11.md) §"The 07:00 window").
**Related:** [spec-proactive-delivery-monitor-2026-06-10.md](spec-proactive-delivery-monitor-2026-06-10.md)
(evidence semantics), U2 broken-briefing drill scheduled ≥ 2026-06-18.

---

## 1. What broke

OpenClaw 2026.6.1 (installed on the pod host Jun 3 14:10) removed the gateway's
`POST /api/message` plain-send endpoint — verified live: 404 on two bots'
gateways with valid tokens; the route literal no longer exists in the installed
`dist/`. Every gallery app's send helper (`get_gateway()` + `send_message()`,
embedded per app by convention and specified in each `build_spec`'s "Gateway
delivery" section) POSTs to that endpoint. Launchd jobs exit 1 at send time, no
run file is written, and because the deployed instances declare no delivery
evidence, the delivery monitor classifies every window `unmeasurable` (probe
error: `no delivery evidence declared (run_file/signal_line)`) instead of
surfacing a miss.

## 2. Surfaces evaluated (all verified on the pod, OC 2026.6.1)

| Surface | Verdict | Evidence / reasoning |
|---|---|---|
| `POST /tools/invoke` + `message` tool | **Rejected** | Endpoint works, but the `message` tool is not policy-exposed (404 "Tool not available"). Exposing it requires `tools.allow: ["message"]` per bot — which also hands the bot's **agent LLM** a proactive arbitrary-channel send tool. That is a security-posture change (the endpoint is documented as a full-operator surface; tool policy is shared between HTTP and agent turns), plus pod-wide config drift risk and new-provision coupling. Same channel/target data dependency as the CLI, so no simplicity win. |
| `openclaw agent --message … --deliver` | **Rejected for plain sends** | Runs a full agent turn: 25–40 s, an LLM call per scheduled send (violates the cheap-infrastructure rule), and nondeterministic output (defer_runner needs "output ONLY the text inside <deliver>" framing). Remains the right surface for agent-mediated follow-ups (defer_runner keeps using it). |
| `openclaw message send` CLI | **CHOSEN** | Plain deterministic channel send, no agent turn, no LLM. Zero config change pod-wide — verified working today as a real bot user (`--dry-run` as a telegram-connected team bot resolved `telegram:<chat-id>`, `via: direct`). Runs as the bot user exactly where scheduled scripts already run. `--json` output + exit code give an acceptance contract. Portable to the Linux pod host port (same CLI). Auth is handled by the CLI from the bot's own config — scripts no longer parse gateway port/token out of `openclaw.json` at all. |

Cost of the CLI choice: a node process per send (~1–2 s startup) — irrelevant at
scheduled-send frequency.

## 3. The convention

Scripts compose their text, then deliver via:

```
openclaw message send --channel=<ch> --target=<target> --message=<text> --json
```

invoked as the bot user (which scheduled scripts already are). Exit code 0 =
the channel accepted the send; only then is the run file written. The `=` form
is mandatory for `--target` (telegram group ids are negative numbers and would
parse as flags otherwise).

Apps keep embedding a copy of the helpers (standalone-stdlib script contract is
load-bearing: any bot, no deps beyond python3 + the openclaw CLI). The canonical
copy lives in
[docs/reference/capabilities/template/scripts/example_script.py](reference/capabilities/template/scripts/example_script.py);
every package `build_spec` carries the same text in its "Delivery" section
(renamed from "Gateway delivery").

### Canonical helper (embedded per app)

```python
OPENCLAW_BIN_CANDIDATES = (
    "/opt/homebrew/bin/openclaw",   # macOS arm64
    "/usr/local/bin/openclaw",      # macOS x86_64
    "/usr/bin/openclaw",            # Linux
)

def find_openclaw() -> str:
    import shutil
    found = shutil.which("openclaw")
    if found:
        return found
    for candidate in OPENCLAW_BIN_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    raise RuntimeError("openclaw CLI not found")


# Channel preference when a user is reachable on several. Order is a
# product default (most personal first), not a technical constraint.
CHANNEL_PRIORITY = ("telegram", "whatsapp", "signal", "imessage", "slack",
                    "discord", "sms", "matrix")

def enabled_channels() -> set | None:
    """Channels this bot can actually send on (its own openclaw.json,
    channels.<name>.enabled). None when unreadable — trust external_ids
    alone rather than refusing to send."""
    cfg = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".openclaw" / "openclaw.json"
    try:
        channels = json.loads(cfg.read_text()).get("channels", {})
    except (OSError, json.JSONDecodeError):
        return None
    enabled = {name for name, c in channels.items()
               if isinstance(c, dict) and c.get("enabled")}
    return enabled or None


def resolve_route(bot_id: str) -> tuple[str, str] | tuple[None, None]:
    """(channel, target) for the bot's primary user, from network.json.

    network.json `bots.<id>.primary_user.external_ids` is the single
    source of truth for where this bot's person lives (same source
    breakers_enforce uses), intersected with the channels this bot has
    enabled — a user recorded on telegram+slack must not be telegram-
    routed by a slack-only bot. Returns (None, None) when the bot has
    no recorded delivery route — callers must degrade gracefully and
    MUST NOT write a run file.
    """
    network = load_network()
    ids = (((network.get("bots") or {}).get(bot_id) or {})
           .get("primary_user") or {}).get("external_ids") or {}
    enabled = enabled_channels()
    for channel in CHANNEL_PRIORITY:
        target = ids.get(channel)
        if target and (enabled is None or channel in enabled):
            return channel, str(target)
    # No overlap with enabled channels — fall back to the best recorded
    # id so the send fails LOUDLY (a monitor-visible miss), never a
    # silent skip.
    for channel in CHANNEL_PRIORITY:
        target = ids.get(channel)
        if target:
            return channel, str(target)
    return None, None


def send_message(bot_id: str, text: str) -> bool:
    """Deliver text to the bot's primary user. True = channel accepted
    the send; False = no delivery route configured (graceful skip);
    raises RuntimeError on a failed send. Run files are written by the
    caller ONLY on True."""
    import subprocess
    channel, target = resolve_route(bot_id)
    if channel is None:
        print(f"DELIVERY_SKIPPED: no delivery route for {bot_id} "
              "(network.json primary_user.external_ids empty)", file=sys.stderr)
        return False
    openclaw = find_openclaw()
    cmd = [
        openclaw, "message", "send",
        f"--channel={channel}", f"--target={target}",
        f"--message={text}", "--json",
    ]
    # launchd/systemd jobs get a minimal PATH; the openclaw entrypoint is
    # `#!/usr/bin/env node`, so prepend the CLI's bin dir (where node also
    # lives) to the child PATH.
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(openclaw) + os.pathsep + env.get("PATH", "")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                       cwd=pwd.getpwuid(os.getuid()).pw_dir, env=env)
    if r.returncode != 0:
        err = (r.stderr.strip() or r.stdout.strip())[:400]
        raise RuntimeError(f"openclaw message send failed (rc={r.returncode}): {err}")
    return True
```

Notes:

- `cwd` is the executing account's pwd-database home, NOT `$HOME`/
  `Path.home()` — node calls `uv_cwd()` at startup and under
  sudo-to-the-bot-account without `-H` the env still carries the invoking user's
  untraversable home (same gotcha as CLAUDE.md's SSH rule).
- 60 s timeout: generous for a plain send; the CLI must not be killed mid-send
  (defer_runner's 2026-05-06 lesson — the process must outlive delivery).
- No gateway port/token parsing anywhere. `get_gateway()` is dead; remove it.

### Send-outcome semantics (the honesty contract)

| Outcome | Script behavior | Run file? |
|---|---|---|
| `send_message` returns True | normal post-send path | **yes** — written atomically after the send |
| returns False (no route) | log `DELIVERY_SKIPPED:` line, exit 0 | **no** |
| raises (send failed) | log app-specific `*_FAILED:` line, exit non-zero | **no** |

The run file's existence remains the claim "the user got it" (delivery-monitor
spec §5.4). A bot with an installed user-facing app but no route produces an
honest miss (`ran_undelivered`) every window — that is correct: it *is* broken,
and the `DELIVERY_SKIPPED` stderr line tells the operator exactly why. Day-1
channel-less bots don't reach this state today (the coherence gate blocks
user-facing app installs without a messaging integration); the wizard/day-1
channel design is a separate design-sync item per the M4/U1 decision doc — this
spec only guarantees the convention degrades honestly if the state arises.

### Route data

`primary_user.external_ids` currently exists for six of the pod's ten bots
(the slack and telegram team/member bots plus the U0 baseline bots); it is
missing for the personal bot, the discord team bot, the day-1 wizard bot, and
the primary (evolve) account. Bots missing it skip gracefully and surface as
misses if they have user-facing apps installed. Backfilling the two bots that
have working channels but no recorded route is a pod-state repair step at
rollout; capturing `external_ids` during provisioning belongs to the day-1
channel design.

## 4. Delivery-monitor evidence fix (the masking bug)

`classify_window` (packages/analyzer/delivery_monitor.py) currently returns
`unmeasurable` whenever the delivered-probe reports any error — including
"no delivery evidence declared", and *before* consulting ran-evidence. The
launchctl probe already collects `LastExitStatus` but classification ignores it.
That is how eight days of exit-1 crashes read as "can't tell".

Change (spec §6.2 refinement, same tri-state philosophy):

1. "No delivery evidence declared" stops being a probe *error*; it becomes
   `delivered=undeclared`, a first-class evidence state.
2. `last_exit` (from the existing `launchctl list` probe) joins classification
   for launchd-family mechanisms, paired with in-window ran-evidence.
3. New ordering, after on-time/late checks:
   - delivered-probe hard error (EACCES, sudo denied…) → `unmeasurable` (unchanged);
   - `ran=True` and `last_exit != 0` → `missed` / `ran_undelivered`,
     `suspected_cause="script_error"` (carries the exit status in details) —
     **regardless** of whether delivery evidence is declared. A scheduler-fired
     script that exited non-zero before producing delivery proof did not deliver.
   - `ran=True`, `last_exit == 0`, delivered undeclared → `unmeasurable`
     (genuinely can't confirm — unchanged outcome, now reached honestly);
   - `ran=None` → `unmeasurable`; `ran=False` → `missed` / `did_not_run` —
     note this IS a behavior change for undeclared-evidence apps (previously
     unmeasurable): a no-fire window needs no delivery evidence, and a
     booted-out legacy instance now fires an honest miss. Heal stays gated
     on a declared `heal: rerun`, so undeclared apps are never auto-rerun.
   - `last_exit` guards: the value is launchd's job-global most recent
     COMPLETED exit — when launchctl shows a PID (a run in flight), it is
     ignored (it belongs to a previous run), and the raw wait-status
     encoding (exit N ⇒ N·256) is normalized back to N before use.
     Residual imprecision: for interval jobs a run later in the window can
     overwrite the exit of the run being judged — acceptable because the
     false-miss direction is rescued by the late-delivery recheck and the
     false-green direction requires a crash followed by a clean run inside
     one window.

The heal path is untouched: `script_error` misses are not heal-eligible
(kickstart would re-run the same crashing script; the fix is the app, not the
scheduler). `suspected_cause="script_error"` is excluded from kickstart the
same way `dst`/`host_asleep` are.

## 5. ea-pack measurability

The ea-pack scripts produce no delivery evidence at all (instance manifests
declare `delivery_contract: null`). As part of the helper migration the three
user-facing ea-pack scripts (morning_brief, evening_sweep, pre_meeting_brief)
write run files at `memory/ea-runs/<action-id>/{date}.json` (same shape as the
briefing run record), so instances can declare `run_file` evidence. The
deployed instance manifests get `delivery_contract` backfilled in the rollout
repair pass; the monitor's §4 fix keeps them honest even before that lands.

## 6. Rollout / repair plan (the pod)

0. **CLI device scope grant (discovered during rollout, applies to every
   bot):** the OC 2026.6 upgrade narrowed each bot's own CLI device to
   `operator.read` in `~/.openclaw/devices/paired.json` — `openclaw message
   send` (and any CLI-via-gateway surface, including defer fires) dies with
   "scope upgrade pending approval", and the approval flow cannot self-serve
   (each CLI attempt files a fresh request that supersedes the last). Repair:
   edit the bot's `paired.json` CLI-device entry to scopes
   `[operator.read, operator.write, operator.pairing]` (also inside
   `tokens.operator.scopes`), then restart that bot's gateway
   (`launchctl kickstart -k system/ai.openclaw.<bot>-gateway`). A durable
   deploy-time invariant for this belongs in `ensure_pod_perms`/deploy
   (follow-up).
1. Land this PR; repo-puller picks it up (≤ 15 min).
2. **The U0 baseline bot with ea-pack installed (×3 scripts):**
   `sudo evolve-admin apply-actions <bot> ea-pack --force-files-sync` —
   ea-pack deploys verbatim gallery files, so the sync replaces the dead
   helpers. Then backfill `delivery_contract` on the instance manifest
   (run_file evidence at the §5 paths).
3. **ledger (forge-built morning_briefing.py):** per the M4/U1 decision doc,
   re-run the gallery install once a channel + route exist — it is one gallery
   install against the updated build_spec, not a re-provision. No in-place
   patching of forge-built code.
4. Kickstart `ai.evolve.evolve.delivery-monitor` (long-running daemon + git
   pull = stale module cache).
5. **Verification (the actual P0 exit criterion):** trigger one real scheduled
   send (kickstart `ai.evolve.<bot>.ea-morning` on the repaired bot), confirm
   the Telegram message arrives, the run file exists, and the next monitor tick
   writes the first `on_time` row ever seen in
   `{shared_dir}/delivery_monitor/ledger/`.
6. Backfill `primary_user.external_ids` for the two bots with working channels
   but no recorded route.

**U2 drill coordination:** the broken-briefing bootout drill (≥ Jun 18) assumed
a *working* baseline to break. The canary soak that started Jun 11 measured a
dead transport — restart the soak clock from the day step 5 verifies, and run
the drill after at least one clean on_time day.

## 7. Out of scope (tracked elsewhere)

- Day-1 channel connect in the wizard / deferred briefing self-scheduling —
  design sync per M4/U1 decision doc finding 4.
- Coherence-gate behavior for channel-less bots (same finding).
- `external_ids` capture at provisioning time.
- A shared installed library for app scripts (embedded-copy stays the contract;
  revisit only if a third mass migration happens).
