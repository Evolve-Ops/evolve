# Wizard Verification Gauntlet — Spec

**Status:** draft (2026-05-30)
**Calibrated against:** atlas-onboarding follow-on incidents 2026-05-29 → 2026-05-30 — six bugs the wizard silently shipped over
**Companion docs:**
- [docs/spec-add-bot-wizard-2026-05-28.md](spec-add-bot-wizard-2026-05-28.md) — the wizard this gauntlet bolts onto (PR α/β/γ)
- [packages/admin/evolve_admin/web/wizard_routes.py](../packages/admin/evolve_admin/web/wizard_routes.py) — existing `/api/wizard/*` surface
- [packages/admin/evolve_admin/deploy.py:set_evolve_read_acl](../packages/admin/evolve_admin/deploy.py) — the canonical "what should be owned/ACL'd how" reference
- [packages/admin/evolve_admin/openclaw_config_validator.py](../packages/admin/evolve_admin/openclaw_config_validator.py) — pattern for running `openclaw config validate --json` from the admin server
- [packages/admin/evolve_admin/health.py](../packages/admin/evolve_admin/health.py) — `_http_ok()` + gateway probe pattern reused by Check 2

---

## 0. Purpose

The 5-screen Add-a-Bot wizard ([spec-add-bot-wizard-2026-05-28.md](spec-add-bot-wizard-2026-05-28.md)) declares **success** as soon as the 6-stage provision pipeline returns OK and the operator clicks past the Review screen. Atlas was provisioned through that wizard on 2026-05-29 and the wizard declared success — but six distinct bugs surfaced over the next 24 hours that each individually blocked the bot from working:

| # | Bug | PR |
|---|---|---|
| 1 | Auth-profile key shape (`anthropic_api_key` vs `anthropic:api_key`) | [#1752](https://github.com/evolve-ops/evolve/pull/1752) |
| 2 | No `model.primary`, falling back to OpenAI | [#1701](https://github.com/evolve-ops/evolve/pull/1701) / [#1703](https://github.com/evolve-ops/evolve/pull/1703) / [#1707](https://github.com/evolve-ops/evolve/pull/1707) |
| 3 | Manifest write failed during forge install | [#1775](https://github.com/evolve-ops/evolve/pull/1775) / [#1781](https://github.com/evolve-ops/evolve/pull/1781) |
| 4 | Telegram preflight false negative | [#1787](https://github.com/evolve-ops/evolve/pull/1787) |
| 5 | `pairing-allowFrom` file root-owned → first DM EACCES | [#1815](https://github.com/evolve-ops/evolve/pull/1815) |
| 6 | `auth-profiles.json` parent dir root-owned → every DM EACCES | [#1816](https://github.com/evolve-ops/evolve/pull/1816) |

Every single one would have been caught **before the wizard declared success** if the wizard had verified that the bot can actually respond, not just that configuration files were written. The atlas operator's note:

> "This was a bad experience for a bot that was installed via Evolve. Should 'just work' when it is installed that way and not have errors or issues that need to be troubleshooted like this."

The verification gauntlet is the structural answer. It replaces "the provision pipeline reported success" with "the bot's full message-handling path has been demonstrated to work". The same gauntlet is also exposed as a **Verify** action on every bot tile so operators can re-run it any time (after a deploy, when troubleshooting, before adding integrations).

This is verify-or-don't-ship applied to bot creation — the same pattern Tailscale uses for "did you actually join the network?" (the green checkmark only flips when an actual ping succeeds, not when the daemon starts).

---

## 1. The four checks

| # | Check | Cost | Trigger | Catches |
|---|---|---|---|---|
| 1 | Ownership audit | cheap | automatic | #1815, #1816 — files / dirs root-owned that the bot can't read |
| 2 | Agent dry-run | medium | automatic | #1701/#1703/#1707, #1752 — auth-profile key shape, missing primary model, gateway-load errors |
| 3 | Channel handshake re-check | cheap | automatic | rotated / revoked tokens; #1787-style preflight gap |
| 4 | End-to-end echo | slow | operator action | the **whole** message-handling path; catches anything 1+2+3 miss in the integration seam between OC, the channel adapter, and the agent loop |

Success = all four green. Any failure surfaces the **specific** failure path with the actual error from the gateway / log (not a generic "something went wrong"), and offers a one-click **Fix and re-verify** where automation can repair, or a **Help** link to the right page where it can't.

### Check 1 — Ownership audit

**Goal.** Detect any file or directory under `/Users/<bot_user>/.openclaw/` (and a handful of out-of-tree paths the bot needs, e.g. `~/.zshrc`) that is owned by `root` (or any uid that isn't the bot user). Also verify the inherited `+a evolve allow …` ACL is present on the dirs where [`set_evolve_read_acl()`](../packages/admin/evolve_admin/deploy.py) puts it.

**Implementation.**

```python
# evolve_admin.wizard.verify.check_ownership(bot_id, network) -> CheckResult
import pwd, os
from pathlib import Path

bot_user = get_bot_user(bot_id, network)
expected_uid = pwd.getpwnam(bot_user).pw_uid

issues: list[dict] = []
oc_root = Path(f"/Users/{bot_user}/.openclaw")
# Walk via os.walk; admin has macOS ACL read on .openclaw/ via
# set_evolve_read_acl, so this is a plain stat — no sudo needed.
for dirpath, dirnames, filenames in os.walk(oc_root):
    for name in dirnames + filenames:
        p = Path(dirpath) / name
        try:
            st = p.lstat()
        except (PermissionError, FileNotFoundError):
            continue
        if st.st_uid != expected_uid:
            issues.append({
                "path": str(p),
                "actual_uid": st.st_uid,
                "actual_owner": _uid_to_name(st.st_uid),
                "expected_owner": bot_user,
            })
```

**Carve-outs.** Some paths are *intentionally* not bot-owned and must be excluded from the audit. Two mechanisms:

*Path-prefix exemptions* (`_OWNERSHIP_EXEMPT_RELATIVE_PREFIXES`) — for directory subtrees and named files at known locations:

- `~/.openclaw/plugins/evolve-plugin/dist/` and the linked `/Users/Shared/evolve-plugin/dist/` — `root:wheel` 755 by design (plugin code must not be writable by the bot).
- `~/.openclaw/workspace/evolve/` — `evolve:staff`; admin owns this, bot has ACL read.
- `~/.openclaw/workspace/evolve-backup/` — same as above.
- `~/.openclaw/workspace/manifests/` — `<bot>:staff` but contents may be `evolve:staff` (admin writes manifests there with workspace/evolve-style ACL); exempt anything created post-deploy.
- `~/.openclaw/workspace/INSTALLED_APPS.md` — forge install bookkeeping; admin-written.
- `~/.openclaw/workspace/POD_CONDUCT.md` — sysadmin conduct injection (via session_surface); admin-written.
- `~/.openclaw/workspace/AGENTS.md` — per-bot conduct; admin-rendered.
- `~/.openclaw/workspace/RUNTIME_NOTES.md` — pod-wide tactical-runtime channel; admin-written.
- `~/.openclaw/workspace/.git/` — write-by-many (bot agents commit, the puller cloud-syncs, operators run ad-hoc commands); per-object ownership inside git internals is intentionally heterogeneous. The bot's runtime doesn't read these objects directly. Walking in produces dozens of findings on every healthy bot (atlas had 99 of 100 findings here on 2026-05-30).

The four workspace-root files above were added 2026-05-30 after the gauntlet's first live pass surfaced `workspace/INSTALLED_APPS.md owner=evolve` on atlas — a false positive because that file is admin-written by design. Bot-owned overrides of these files are uncommon but legal; the gauntlet's contract is "don't flag the file at all", not "flag unless evolve-owned", because a root-owned `INSTALLED_APPS.md` is an admin-server bug, not a bot-config bug, and is out of scope for the verify gauntlet.

*Basename-pattern exemptions* (`_OWNERSHIP_EXEMPT_BASENAME_PATTERNS`) — for files whose location varies but whose name follows a well-known pattern:

- `openclaw.json.bak[.-]*` — operator-driven backup snapshots created via `sudo cp` before invasive edits. Root-owned by definition (the `sudo cp` preserves root as the new file's owner). Harmless — the bot reads only `openclaw.json`. Multiple bots hit this on 2026-05-30.
- `openclaw.json.pre[.-]*` — same shape, different naming convention used by older migration scripts.

Kept narrow on purpose so we don't paper over real bugs. When adding a new pattern, also update the corresponding test parametrization in `tests/test_wizard_verify.py::test_is_exempt`.

**ACL re-check.** After the ownership pass, re-run `set_evolve_read_acl(bot_id)` in dry-run mode (a thin wrapper that runs `ls -le` and parses the `+a` entries, comparing against the expected ACE list). Any missing ACE is a finding.

**Fix.** For each finding, offer a single button "Fix ownership". Under the hood:

```python
subprocess.run(
    ["sudo", "/usr/sbin/chown", "-R", f"{bot_user}:staff", path],
    capture_output=True, timeout=10,
)
# Then re-run set_evolve_read_acl(bot_id) to reapply the inherited ACL.
```

Requires a new sudoers grant `evolve ALL=(root) NOPASSWD: /usr/sbin/chown -R * /Users/*/.openclaw` (mid-path wildcard accepted by macOS visudo; no trailing `/*`). Existing targeted grants for `agent/` and `auth-profiles.json` stay as a defense-in-depth narrow path; the new broad grant catches the rest.

### Check 2 — Agent dry-run

**Goal.** Prove that, end-to-end, the bot's configuration is consistent enough that the agent can resolve a primary model + credential pair and the gateway has loaded the plugin without errors. Stops short of issuing a real Anthropic API call (that's where Check 4 takes over) but does everything that doesn't burn tokens.

**Implementation — four sub-steps, in order; failure short-circuits.**

1. **Gateway liveness.** Same probe as `health._probe_evolve_plugin_loaded(port)` — `GET http://127.0.0.1:{port}/evolve/status`, expect 200 with JSON containing one of `bot_id` / `plugin_version` / `status`. A 200 with HTML is "gateway up but plugin not loaded" (OC's SPA fallback); both 200-HTML and non-200 are failures.

2. **`openclaw config validate --json`.** Reuse [`openclaw_config_validator.validate_bot_openclaw_json()`](../packages/admin/evolve_admin/openclaw_config_validator.py). Runs as `evolve` with `OPENCLAW_CONFIG_PATH={oc_json}`, `cwd=/tmp`. Catches schema rejections (PR #1525-shape regressions).

3. **Primary-model resolution.** Read `openclaw.json` and verify `agents.defaults.model.primary` is set AND the named model exists in the bot's catalog (`oc_model.py`'s `list_models()` via the existing sudo grant). The atlas-shape regression where `model.primary` was absent and OC fell back to a default OpenAI model is caught here.

4. **Credential-shape match.** Read `auth-profiles.json` via the existing `/bin/cat /Users/*/.openclaw/agents/main/agent/auth-profiles.json` sudoers grant. For each model required by `agents.defaults.model.*`, verify the credential the model needs is present and uses the **correct key shape**:
   - `anthropic:api_key` — NOT `anthropic_api_key` (the [#1752](https://github.com/evolve-ops/evolve/pull/1752) bug)
   - `anthropic:auth_token` — accepted alternative for OAuth flows
   - `openai:api_key`, `brave:api_key`, etc. — colon-separated `provider:slot`
   
   Detect underscore-only forms (`anthropic_api_key`) and flag as `"key_shape_legacy"` with the specific path to the malformed entry. We do NOT auto-fix this — credential repair is an operator decision.

**Error surfacing.** When a sub-step fails, the gauntlet captures the most recent ~60 lines of `~/.openclaw/logs/gateway.err.log` (existing `/bin/cat …/logs/gateway.err.log` sudoers grant) and includes them in the result. Operators see the EACCES / 401 / "model not found" line that caused the failure inline, not a generic "agent dry-run failed". The log tail is filtered to entries newer than the wizard's provision start timestamp so older noise doesn't leak in.

**Timeout.** 30s total for the gateway-liveness probe (first call after gateway start can be slow); 10s each for the other steps.

**Future extension (out of scope for this PR).** A real 1-token Anthropic ping that costs a fraction of a cent and proves the credential actually validates against the upstream API. Discussed in §6. Not in v1 because it costs real money + requires a careful "are we sure this credential is OK to use" gate.

### Check 3 — Channel handshake re-check

**Goal.** For every channel the bot is configured for, re-validate the credential against the upstream API. Tokens can rotate or be revoked between wizard input and the moment the bot tries to use them; the wizard's Screen 4 token check is a snapshot.

**Implementation.** Per-channel, mirrors the install-time validation code that already lives elsewhere:

- **Telegram.** `GET https://api.telegram.org/bot<token>/getMe`. Expect `result.username` non-empty. If `network.json::bots.<bot>.channels.telegram.username` is recorded, assert it matches.
- **Slack.** `POST https://slack.com/api/auth.test` with `Authorization: Bearer <token>`. Expect `ok: true`. Reuse `skills/slack_install.SLACK_AUTH_TEST_URL` constant.
- **Discord.** `GET https://discord.com/api/v10/users/@me` with `Authorization: Bot <token>`. Expect HTTP 200 with `id` field.
- **WhatsApp / Signal / others.** Out of scope for v1 — flag as `"channel_check_unsupported"` and fall through (operator can still ship Verify with a yellow `"unverified"` chip for that channel).

Tokens are read from the bot's `auth-profiles.json` via the existing sudo grant. The check itself runs as the `evolve` user — outbound HTTPS only, no credential surface beyond the in-memory token.

**Timeout.** 8s per channel. All channels run in parallel (`concurrent.futures.ThreadPoolExecutor`).

**No fix automation.** A failed channel check means the token is bad. We surface the specific upstream error (Telegram: `description: "Unauthorized"`; Slack: `error: "invalid_auth"`; Discord: 401) and deep-link to the bot's Channels page where the operator can rotate the token. No auto-fix is safe here.

### Check 4 — End-to-end echo

**Goal.** Prove the full message-handling path: messaging-platform delivery → channel adapter → OC dispatch → agent loop → response generation → delivery back to the operator. This is the only check that exercises every layer, and it's the only check that would have caught the atlas sequence cleanly.

**Implementation.**

1. **Inject the contract.** The gauntlet writes a one-shot conduct-injection block to the bot's `~/.openclaw/workspace/POD_CONDUCT.md` (or the runtime-notes equivalent) that says:
   > **Verification echo (wizard request).** Until `<expiry>`, if you receive the literal text `ok` from the configured operator, reply with the literal text `Verified — <bot_name> ready.` Once you reply, this contract is satisfied; you may delete it from POD_CONDUCT.md on your next turn.
   
   `expiry` is now + 6 minutes. Writes go through the standard POD_CONDUCT injection channel (`session_surface.py`) — no special privilege needed.

2. **Show the operator the prompt.** The wizard's Check-4 row says:
   > DM `@<bot>` the literal text **`ok`**. I'll flip this to ✓ when I see the bot's reply.
   
   For bots with no messaging channel configured, Check 4 is skipped with a yellow `"unverified — no channel"` chip on the row. The Done button still lights up — Check 4 is not a hard gate (we can't force operators to send messages).

3. **Watch for the reply.** Backend polls the bot's turns log (`~/.openclaw/agents/main/agent/turns/<YYYY-MM-DD>.jsonl` — existing `/bin/cat …/turns/*` sudo grant) every 2s, scanning for a turn where `output.text` contains `Verified — <bot_name> ready`. We also accept reasonable variants (case-insensitive match on the literal anchor `Verified — <bot_name> ready` to avoid false negatives from a reply like `"Verified — atlas ready."` with a trailing period).

4. **Timeout.** 5 minutes. On timeout, the row turns yellow with text:
   > Didn't see a reply within 5 minutes. The bot might still be working — check the gateway log, or skip Verify and revisit later.
   
   Offer **Retry** (re-arm the contract for another 5 min) and **Skip** (continue with a yellow `"e2e unverified"` chip on the bot tile).

5. **Cleanup.** After success (or skip), remove the verification-echo block from POD_CONDUCT.md so it doesn't leak into future turns.

**Why operator action is the right design for v1.** A future "the wizard DMs the bot itself from another bot" path is conceivable, but it's not honestly stronger — it just moves the trust boundary. The operator-DMs-the-bot version proves the channel works *for the actual user*, which is the truthful gate. Tailscale's "ping a peer" flow makes the same trade. The "automatic e2e" version is in §6.

**Notes for primary bots.** When the bot is `primary` (i.e., evo), it can be verified via the admin UI's evo-chat panel instead of an external messaging channel — the wizard offers a "type `ok` here" inline input box that posts to the same evo chat endpoint the dashboard already uses. Same contract, same reply matcher.

---

## 2. Wiring into the wizard

Replace the current Screen 5 "All set / Done" with **Screen 5: Verify**. The Done button on the gauntlet's Screen 5 is disabled until all automatic checks pass; Check 4 is optional (operator can skip with a yellow chip on the bot).

```
☐ Files written              ✓
☐ Ownership correct          ⏳ verifying...
☐ Agent responds             ⏳
☐ Channels reachable         ⏳
☐ End-to-end echo            (waiting for you to DM @<bot> "ok")

[ Skip Verify ]   [ Done ]    ← Done disabled until rows 1-4 are ✓ or skipped
```

Each row is independently retryable. The row icon updates from `⏳` to ✓ / ⚠ / ✗ as the backend reports. Inline detail expands on click — operators see the actual gateway.err.log line or the actual `getMe` response, not "something went wrong".

On any failure:
- Row turns yellow with one-sentence summary.
- Expanded detail shows the captured error.
- **Fix** button where automation can repair (ownership only, today).
- **Help** link otherwise (deep-link to the Credentials page for auth issues; the Channels page for token rotations; an internal `docs/troubleshooting-wizard-verify.md` for novel failures).

The wizard's "All set" copy from the old Screen 5 moves to a final overlay shown after Done — same summary, same Next-steps copy, same "install apps from gallery" CTA.

---

## 3. Verify button on every bot tile

The same gauntlet — minus Check 4 (which is offered separately, on demand) — is exposed as a `🔍 Verify` action button on every bot tile. Lives in the `pod-actions` action row next to Redeploy / Restart / breaker button. Click opens a modal showing the same 4-row checklist; auto-runs Checks 1-3 immediately. Check 4 is a "Run end-to-end test" link that, on click, arms the verification-echo contract and shows the same "DM `@<bot>` `ok`" prompt as the wizard.

The bot tile's existing `tile.health_chips` already conveys "bot is sick"; the Verify modal is the "tell me *what* is sick" drill-down.

A failed gauntlet leaves a sticky `unverified` chip on the tile until the operator clicks Verify again. This is informational only — does not block deploys, does not trip breakers. (The signal store catches the *consequences* of unverified-bad-config via the existing monitors; this is the operator-driven proactive check.)

---

## 4. Backend module + endpoints

New module `evolve_admin.wizard.verify` (~400 lines). Pure-Python, no LLM, no external deps beyond what's already in the admin runtime.

```python
# evolve_admin/wizard/verify.py

# Public surface:
def run_gauntlet(
    bot_id: str,
    *,
    network: dict,
    include_e2e: bool = False,
    e2e_session_id: str | None = None,
    network_path: Path | None = None,
) -> GauntletResult: ...

def repair_ownership(bot_id: str, path: str, *, network: dict) -> RepairResult: ...

def arm_e2e_contract(bot_id: str, *, network: dict) -> str:
    """Write the verification-echo block to POD_CONDUCT.md.
    Returns the e2e_session_id (used by the poller to match the right reply)."""

def disarm_e2e_contract(bot_id: str, e2e_session_id: str, *, network: dict) -> None: ...

@dataclass
class CheckResult:
    name: str                    # "ownership" / "agent_dry_run" / "channels" / "e2e_echo"
    status: str                  # "ok" / "warn" / "fail" / "skip" / "pending"
    summary: str                 # one-line for the row
    detail: str | None = None    # multi-line, shown on expand
    issues: list[dict] = field(default_factory=list)
    fix_available: bool = False  # set True for ownership findings
    fix_hint: str | None = None  # deep-link target or sentence-of-advice

@dataclass
class GauntletResult:
    bot_id: str
    started_at: str
    finished_at: str | None
    checks: list[CheckResult]
    overall: str                 # "ok" / "warn" / "fail"
```

### Endpoints (registered in `wizard_routes.register_wizard_routes`)

| Endpoint | Behavior |
|---|---|
| `POST /api/wizard/verify/start` | Body `{bot_id, include_e2e?}`. Returns `{jobId, status: "started"}`. Backgrounds the gauntlet via the existing `_new_job` mechanism. |
| `GET /api/wizard/verify/<job_id>` | Returns the latest `GauntletResult` for that job. Frontend polls every 1.5s. |
| `POST /api/wizard/verify/<bot_id>/fix-ownership` | Body `{path}`. Runs `repair_ownership(bot_id, path)`. Returns `{ok, before, after}`. |
| `POST /api/wizard/verify/<bot_id>/arm-e2e` | Body `{}`. Calls `arm_e2e_contract(bot_id)`. Returns `{ok, e2e_session_id, expiry, channel}`. |
| `GET /api/wizard/verify/<bot_id>/e2e-status?session=<id>` | Returns `{status: "waiting"|"verified"|"timeout", reply_seen_at?}`. Frontend polls every 2s. |
| `POST /api/wizard/verify/<bot_id>/disarm-e2e` | Body `{session_id}`. Tear down the POD_CONDUCT block (called by both success and skip paths). |

All endpoints assume the admin server's auth boundary is the surface (per the existing wizard's design). No per-role gates.

### Module layout

```
packages/admin/evolve_admin/wizard/
├── __init__.py
└── verify.py         # ~400 LOC, public surface above
```

(Note: `evolve_admin.wizard` is a new package; the existing wizard work lives in `evolve_admin.evo.wizard` (the chat onboarding) and `evolve_admin.web.wizard_routes` (the form-driven add-bot). This module is a sibling of `web/wizard_routes.py` — same problem domain — but lives one level up so future verify primitives have a clean home.)

---

## 5. Sudoers grants

Existing grants already cover almost everything the gauntlet needs:

| Operation | Existing grant | Sufficient? |
|---|---|---|
| Read `openclaw.json` | `/bin/cat /Users/*/.openclaw/openclaw.json` | ✓ |
| Read `auth-profiles.json` | `/bin/cat /Users/*/.openclaw/agents/main/agent/auth-profiles.json` | ✓ |
| Read `gateway.log` / `gateway.err.log` | `/bin/cat /Users/*/.openclaw/logs/gateway.{log,err.log}` | ✓ |
| Read `turns/*.jsonl` | `/bin/cat /Users/*/.openclaw/agents/main/agent/turns/*` | ✓ |
| `openclaw config validate` | `evolve ALL=(ALL) NOPASSWD: SETENV: <openclaw_bin>` | ✓ |
| Chown agent dir contents | `/usr/sbin/chown * /Users/*/.openclaw/agents/main/agent` etc. | ✓ for that path only |
| Chown anything else under .openclaw/ | — | **NEW grant needed** |

The new grant — added in `setup_wizard._render_evolve_sudoers()`:

```
# ── 6c. Ownership repair under .openclaw/ (wizard verify gauntlet) ──────────
# The verification gauntlet's Check 1 walks the entire .openclaw/ tree and
# flags any path not bot-owned. The Fix button chowns just the offending
# path back to bot:staff. The existing line 2053 grant only covers
# agents/main/agent; this extension covers the rest of the tree without
# expanding to /Users/*/* (which would let evolve chown anything in a bot's
# home, breaking the "evolve can read but not own bot home" invariant).
#
# Mid-path /.openclaw wildcard is accepted by macOS visudo; trailing /* is
# not, so we use a recursive-form grant with the path component before /.
evolve ALL=(root) NOPASSWD: /usr/sbin/chown -R * /Users/*/.openclaw
evolve ALL=(root) NOPASSWD: /usr/sbin/chown * /Users/*/.openclaw
```

Both forms — recursive and per-file — are needed because some ownership fixes target a single file (e.g. `pairing-allowFrom`) and some target a directory.

The grant is validated against the existing visudo-check path before install. The existing CI test `test_sudoers_visudo_validates` (if present; if not we add it) re-validates.

---

## 6. Out of scope for this PR

These come up naturally in design conversations; deferring them is deliberate.

- **Automatic e2e echo (some other bot DMs the new one).** Conceivable, but not honestly stronger than operator-DMs-the-bot — moves the trust boundary without proving more. Worth revisiting when there's a clear consumer (e.g. CI test that wants no human in the loop). Out of scope for v1.
- **Real 1-token Anthropic ping in Check 2.** Would catch credential-revoked scenarios that the structural check misses. Two reasons it's deferred: (1) it costs real money on every Verify click — needs a "are you sure?" gate or a rate limit, and (2) for bots configured with multiple providers (anthropic + openai + brave) we'd want to ping each, which scales the cost. Tracked as a follow-on chip; design when we have the first real "I rotated my key and didn't know my bot was broken until tomorrow" report.
- **WhatsApp / Signal / other channel checks.** Telegram + Slack + Discord covers the channels the wizard actually installs today. As channel support expands, each new channel adds a 5-line `_check_<channel>(token)` to `verify.py`.
- **Fixing wizard bugs the gauntlet would have caught.** [#1815](https://github.com/evolve-ops/evolve/pull/1815) and [#1816](https://github.com/evolve-ops/evolve/pull/1816) are already in flight as separate PRs. The gauntlet's job is to CATCH them next time, not patch them. (And if a future regression slips, the gauntlet flips red and the operator sees it immediately — that's the point.)
- **Persisting GauntletResult to a per-bot history.** Each Verify run is currently ephemeral (job-state only). A future iteration could write a Verify history to `{shared_dir}/wizard-verify/<bot>/<timestamp>.json` for trend-reading. Not needed for v1.
- ~~**Auto-running Verify on a schedule.**~~ **Landed as `install_integrity_monitor`** (follow-up PR, see [packages/analyzer/install_integrity_monitor.py](../packages/analyzer/install_integrity_monitor.py)). Runs the gauntlet's automatic checks (1+2+3) per bot once a day; emits a Signal per non-OK finding with stable signatures so repeat findings dedupe. Auto-resolves via `signals.store.sweep_resolve()` when the condition clears. Signal types: `install_integrity:ownership_drift`, `install_integrity:gateway_down`, `install_integrity:config_invalid`, `install_integrity:missing_primary_model`, `install_integrity:legacy_credential_shape`, `install_integrity:missing_credential`, `install_integrity:channel_handshake_failed`. Daily cadence is deliberate: the gauntlet's checks are correctness checks, not liveness checks; liveness already has hourly coverage via heal/pod-health.

---

## 7. Failure modes the gauntlet must handle gracefully

Failure modes of the gauntlet itself — these are listed so they're not surprises in implementation:

1. **The bot's gateway is down at Verify time.** Check 1 still runs (file ownership doesn't need the gateway); Check 2 fails on sub-step 1 (gateway liveness) with a clear "no response on port X — check gateway.err.log" message; Check 3 still runs (token checks are outbound, not via the gateway); Check 4 fails to arm (can't write to POD_CONDUCT.md if the bot has no workspace yet) and surfaces a "deploy the bot first" hint.

2. **The bot has no auth-profiles.json yet.** Check 2 sub-step 4 surfaces this as "credentials not yet configured — finish Screen 4 of the wizard first" rather than a confusing parse error.

3. **The bot has a channel configured but the token slot is empty.** Check 3 skips the channel with a yellow `"not configured"` chip on that row.

4. **The operator closes the wizard mid-Check-4.** The armed POD_CONDUCT block stays in place; it expires at the 6-minute timestamp baked in. The next session_surface read after expiry drops the block, so there's no permanent leakage.

5. **Two operators run Verify concurrently on the same bot.** The job IDs are independent; the underlying checks are idempotent for 1/2/3. For Check 4, the e2e_session_id discriminates which contract's reply we're watching for — the bot's reply text matches the most recently armed contract.

6. **The bot replies with the right text BEFORE the operator's `ok` lands (e.g., the operator types `ok` in a different chat first).** False positive risk is low because we anchor on `Verified — <bot_name> ready` which the bot would not produce in normal use; but to be safe we additionally require the matching turn to be NEWER than `arm_e2e_contract`'s timestamp.

---

## 8. Test plan

### Unit tests — `tests/test_wizard_verify.py`

- `test_check_ownership_clean_bot` — fixture with all-bot-owned `.openclaw/` returns `ok` + empty issues.
- `test_check_ownership_finds_root_owned_file` — fixture seeds a root-owned file at `~/.openclaw/auth-profiles.json`, gauntlet returns `fail` with the path in issues + `fix_available=True`.
- `test_check_ownership_respects_exemptions` — fixture seeds an evolve-owned file at `~/.openclaw/workspace/evolve/foo.json`, gauntlet returns `ok` (carve-out hit).
- `test_check_agent_dry_run_gateway_down` — gateway not running → `fail` with "no response on port X".
- `test_check_agent_dry_run_config_invalid` — fixture with bad openclaw.json → `fail` with the validator's issue list.
- `test_check_agent_dry_run_missing_primary` — openclaw.json with no `model.primary` → `fail` with "primary model not configured".
- `test_check_agent_dry_run_legacy_key_shape` — auth-profiles.json with `anthropic_api_key` (underscore) → `fail` with "key_shape_legacy" pointing at the bad entry.
- `test_check_channels_telegram_ok` — mocked `getMe` returns `result.username` → `ok`.
- `test_check_channels_telegram_bad_token` — mocked 401 → `fail` with "Telegram: unauthorized".
- `test_check_channels_skips_unknown_channel` — channel not in the support list → `skip` + chip.
- `test_arm_e2e_contract_writes_pod_conduct` — verify the POD_CONDUCT block + expiry land.
- `test_e2e_status_matches_reply_in_turn_log` — fixture seeds a turn with the expected output, status flips to `verified`.
- `test_e2e_status_timeout_after_5min` — clock-injection fixture; after 5 min returns `timeout`.

Total: ~14 tests, ~250 LOC.

### Integration test — `tests/integration/test_wizard_verify_e2e.py`

One test that wires all four against a fake bot scaffold under `tmp_path`:
- Set up a fake `~/.openclaw/` with the expected layout and ownership.
- Mock the gateway HTTP probe to return a valid `/evolve/status`.
- Mock the openclaw CLI subprocess to return `valid: true`.
- Mock the channel APIs (Telegram getMe etc.).
- Drive a Flask test client through `POST /api/wizard/verify/start`, poll `GET /api/wizard/verify/<job>` until done, assert all 4 rows green.

### Sudoers regression — `tests/test_evolve_sudoers.py`

If a test file already exists for `_render_evolve_sudoers`, add an assertion that the new chown grant string is present in the output and that the rendered file passes `visudo -c -f`. If not, this is the impetus to add one.

---

## 9. Implementation plan

Two PRs, gated on the spec doc landing first.

**PR 1 — Backend module + endpoints + sudoers + tests.**
- New `evolve_admin/wizard/verify.py`.
- New endpoints in `web/wizard_routes.py`.
- Sudoers extension in `setup_wizard._render_evolve_sudoers()`.
- Unit + sudoers tests above.

**PR 2 — Wizard Screen 5 rewrite + bot-tile Verify button + e2e test.**
- Replace the wizard's old Screen 5 with the gauntlet checklist + Done gating.
- Add the `🔍 Verify` button to `renderPodNode()`'s `pod-actions` row.
- New JS module (inline in index.html, matching existing conventions): `_verifyOpenForBot(id)` opens the modal; `_verifyPollJob(jobId)` drives the row updates; `_verifyArmE2E(bot)` + `_verifyPollE2E(bot, session)` for the operator-action step.
- Integration test above.

**Live verification — after both PRs merge.**
Run Verify against each of the 8 bots on the live mini. Report which pass cleanly; for any that fail, file an issue per finding (don't fix unilaterally — flag for operator review). The atlas bot in particular is the canary — it's known to have hit all 6 bugs and any remaining residue should surface here.

### Sizing recap

| Item | LOC estimate |
|---|---|
| `wizard/verify.py` | ~400 |
| New endpoints in `wizard_routes.py` | ~120 |
| Sudoers extension | ~6 lines |
| Wizard Screen 5 rewrite (index.html) | ~150 |
| Bot-tile Verify button + modal (index.html) | ~80 |
| Unit tests | ~250 |
| Integration test | ~80 |

Total: roughly 1000 LOC across two PRs. Worth it — every line of it is debt the operator otherwise pays in support hours when the next atlas-shape bot fails.

---

## 10. Acceptance criteria

- A new bot provisioned through the wizard cannot reach the "Done" button unless Checks 1, 2, 3 are all green (Check 4 optional).
- The atlas-shape bug class (any of #1701/#1703/#1707/#1752/#1787/#1815/#1816) — if it were reintroduced as a regression — would be surfaced by Verify with a specific, actionable error message, NOT a generic failure.
- Running Verify on a healthy bot completes Checks 1+2+3 in under 15 seconds total (the slow path is the gateway probe + validator subprocess).
- Running Verify on a bot with a single root-owned file shows the path, offers "Fix ownership", and after the fix returns to green on re-verify with no operator escalation.
- The Verify modal on the bot tile uses the SAME backend endpoints as the wizard Screen 5 — single source of truth, no parallel implementation.
