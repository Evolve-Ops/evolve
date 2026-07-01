# Multi-user dynamic alias — Spec

**Status:** draft (2026-06-01)
**Companion docs:**
- [docs/spec-correspondence-persona-2026-05-30.md](spec-correspondence-persona-2026-05-30.md) — the static `correspondence` block this spec extends
- [docs/spec-google-integration-paths-2026-05-30.md](spec-google-integration-paths-2026-05-30.md) — Gmail send is the v1 consumer
- [packages/admin/evolve_admin/mcp_bridge/google_tools.py](../packages/admin/evolve_admin/mcp_bridge/google_tools.py) — `_build_from_header` and `_build_signature` (the touch points)

This spec covers the third deliverable of the alias work surfaced in the 2026-06-01 design conversation. Deliverables A (smarter wizard defaults) and B (standalone alias editor) shipped first; C is the schema and runtime change to make the alias rotate by initiating user on multi-user bots.

---

## 0. Why this exists

The static `correspondence` block today is one name per bot, written once. That fits a single-user EA bot: "Jane, assistant to Sam." It does not fit a multi-user bot. The motivating case is `team-bot-a` (multi-user project management — a team bot where several humans hand it work): when `team-bot-a` sends mail on Sam's behalf the From header should read "Sam Riley"; when it sends on Jordan's behalf it should read "Jordan Lee." Today both turns use whatever single name was configured at install time, which makes the bot's mail look mislabelled to recipients.

The pattern the v1 personal-assistant bot validated — "alias = the user's own name, with a soft 'assistant to' marker" — is exactly the pattern multi-user bots need too, just rotating per-turn rather than fixed at install.

---

## 1. Concepts

Three pieces have to move together:

1. **Schema.** The `correspondence` block grows a `mode` field. `mode: "static"` (default; today's shape) keeps `name`/`email_address` fixed. `mode: "per_user"` resolves them from the *initiating user* on the turn.
2. **Initiating-user resolution.** The MCP tool call has to know which user the bot is corresponding *on behalf of* at send time. Today the MCP call carries `bot` only; user identity isn't propagated.
3. **Per-user records.** Multi-user bots have a list of users today via `primary_user` (a single record) — that schema needs to grow to capture multiple users, or `correspondence.per_user` has to be self-contained.

---

## 2. Schema

```jsonc
{
  "bots": {
    "team-bot-a": {
      "multiUser": true,
      "primary_user": { ... },                  // existing — the "default" user
      "correspondence": {
        "mode": "per_user",                     // new — "static" | "per_user"
        "disclosure": "soft",                   // still global to the bot
        "fallback": {                           // optional — used when no
          "name": "Team-Bot-A",                 // initiating user resolves
          "email_address": "team-bot-a@..."     // (e.g. cron-triggered turn)
        },
        "per_user": {                           // map keyed by stable user id
          "u_sam":    { "name": "Sam Riley",  "email_address": "sam@..." },
          "u_jordan": { "name": "Jordan Lee", "email_address": "jordan@..." }
        }
      }
    }
  }
}
```

The existing static shape (`mode: "static"` or no `mode` at all) stays unchanged — single-user bots get no schema migration.

Two open design choices, both worth resolving before implementation:

### 2.1. Where do `per_user` records come from?

**Option A — self-contained.** `correspondence.per_user` carries `name` + `email_address` directly, as shown above. Simple, no schema dependencies, but the operator has to type each user's name and email in the alias editor and keep them in sync if a user's name changes.

**Option B — reference the user roster.** Pod schema grows a `bots.<id>.users[]` array (or extend `primary_user` into `users[]` with one marked primary), each entry carrying `name`/`email_address`. `correspondence.per_user` then becomes a *boolean opt-in* per user (or absent entirely — the runtime just looks each user up by id). Cleaner, but requires the new `users[]` schema.

**Recommendation: B.** The Identity tab already wants a multi-user roster — today multi-user bots only record `primary_user`, and additional users are inferred from turn history (`identity_discovery.discover_candidates`). Promoting the roster to first-class schema serves more than the alias work. C should land after that roster lands.

### 2.2. What's the per-user key?

Two candidates:

- **`pod_user`** — the Unix-account-style identifier that already appears on `primary_user.pod_user`. Stable, human-readable, doesn't change when a user gets a new Slack ID.
- **`channel:external_id` pair** — what `identity_discovery` returns. Already populated.

Recommendation: `pod_user` when present, falling back to a synthetic `<channel>_<external_id>` slug when not. This matches how `primary_user` is keyed today.

---

## 3. Initiating-user resolution

The MCP tool call shape today:

```jsonc
{ "tool": "gmail_send", "arguments": { "bot": "team-bot-a", "to": [...], ... } }
```

For per-user alias to work, `gmail_send` (and the other persona-using tools) need to know *which user is corresponding right now*. Three options:

### 3.1. Option I — pass `as_user` explicitly

The bot's agent passes `as_user: "u_sam"` (or just `as_user: "sam"`) in the tool arguments:

```jsonc
{ "tool": "gmail_send", "arguments": { "bot": "team-bot-a", "as_user": "u_sam", "to": [...], ... } }
```

Pros: zero infrastructure changes. The bot's prompt and conduct can be updated to teach it "when corresponding on behalf of a user, pass `as_user`."
Cons: relies on the bot to get this right. Bots could omit it; the runtime needs a default.

### 3.2. Option II — MCP header for initiator

OpenClaw passes an `originating_user` header on every MCP tool call (derived from the channel/external_id of the turn that triggered the tool use). The MCP server reads it and threads it through.

Pros: deterministic, can't be forgotten by the bot.
Cons: requires OpenClaw upstream changes. Multi-turn flows where the user said "send a thank-you to the hotel from yesterday" make this messy — *which* user's turn is the originator?

### 3.3. Option III — session/turn lookup

The MCP server looks up the bot's current session id, walks its conversation log backward to the most recent human turn, resolves that turn's sender to a `pod_user`.

Pros: no bot-prompt change, no OC upstream change.
Cons: stateful and racy across concurrent turns. Multi-turn EA flows ("send three follow-ups" from one user instruction) get this right; explicit "send on Jordan's behalf" doesn't.

**Recommendation: I + III in combination.** Bot-supplied `as_user` is canonical when present (explicit > implicit); session lookup is the fallback (so bots that don't pass it still do the right thing in the common single-user-per-turn case). Option II as a future hardening once OC supports it natively.

---

## 4. Runtime changes

`_build_from_header` and `_build_signature` (both in `packages/admin/evolve_admin/mcp_bridge/google_tools.py`) gain an `initiating_user` parameter:

```python
def _build_from_header(
    bot_cfg: dict,
    default_sender: str,
    initiating_user: dict | None = None,
) -> str:
    corr = bot_cfg.get("correspondence") or {}
    mode = corr.get("mode", "static")
    if mode == "per_user" and initiating_user:
        user_id = initiating_user.get("pod_user")
        record = (corr.get("per_user") or {}).get(user_id)
        if record:
            return formataddr((record["name"], record.get("email_address") or default_sender))
        # Fall through to fallback / static fields when no per_user record.
    # ... existing static path ...
```

`_build_signature` mirrors this, threading `initiating_user.name` through the disclosure template.

Both functions stay pure — caller resolves the initiating user (per §3) and passes it in. That keeps the runtime testable without mocking OC sessions.

---

## 5. Wizard + alias-editor UX

The standalone alias editor (deliverable B) has a hint for multi-user bots — "alias will rotate per initiating user; configure that here." That hint becomes the entry point to a per-user editor screen:

- The card shows a roster (driven by §2.1 Option B's `users[]` once it lands).
- Each row has the same three fields as the single-user editor (name / send-as / disclosure inherited from the bot).
- Default `name` for each row is the user's recorded `primary_user.name` (for the primary) or the user-roster entry's name.
- A "Fallback" row sets the static `name`/`email_address` used when no initiating user resolves.

The path-C wizard's alias section gains a mode toggle: "One alias for this bot" (default for single-user) vs "Alias rotates by initiating user" (default for multi-user, when the roster has more than one entry).

---

## 6. Tests

Schema + validation:

- `mode: "per_user"` requires a non-empty `per_user` map OR a `fallback` block; otherwise validation fails.
- `mode: "static"` (or absent) keeps the existing required-`name` validation.
- `per_user` keys must match recorded user ids in the bot's roster (when §2.1 Option B lands).

Runtime:

- `team-bot-a` with two users: `gmail_send` with `as_user=u_sam` produces a From header with Sam's name + email; same call with `as_user=u_jordan` produces Jordan's.
- `team-bot-a` with no `as_user`: falls back to session-most-recent-human-turn lookup; produces the right user.
- `team-bot-a` with unknown `as_user`: falls back to `correspondence.fallback`; if absent, raises with a clear error.
- Single-user bots (no `mode` field): produce the same output as today's static path, regardless of whether `as_user` is passed.

Wizard:

- Multi-user bot's alias editor pre-fills per-user rows from the roster.
- Toggling `mode: "per_user"` → `mode: "static"` preserves the previous static name as a recovery hint.

---

## 7. Ethical-disclosure interaction

Deliverable A's discoverability work made the disclosure dropdown more legible. The per-user case raises the stakes: a `disclosure: "none"` on a `mode: "per_user"` block means *every* user's mail goes out looking self-authored. The alias editor should require an extra confirmation when an operator sets per-user mode with `disclosure: "none"`, naming the users by count: "This will send all 4 users' mail without an AI disclosure marker. Continue?"

---

## 8. PR plan

Three small PRs, gated on each other:

| PR | Scope | Blocks |
|---|---|---|
| C.1 | Multi-user roster: extend schema to capture `bots.<id>.users[]` (or `primary_user` → `users[]` with one primary). Migration helper for existing single-user bots. UI surfacing on Identity tab. | Roster needed before per-user alias UI |
| C.2 | Schema + runtime: `mode: "per_user"`, `per_user` map, `_build_from_header`/`_build_signature` initiating-user param. MCP arg `as_user`. Session-lookup fallback. Unit tests. | Roster (C.1) for keying |
| C.3 | Wizard + alias-editor UX: mode toggle, per-user rows, ethical-disclosure confirmation. | C.1 + C.2 |

C.1 is independently useful — the Identity tab already wants a multi-user roster — and unblocks both C.2 and several other long-tailed asks (per-user audit views, per-user message-style preferences).

---

## 9. Out of scope

- **Cross-bot persona memory.** "Sam corresponded with this vendor as 'Sam Riley' on the team bot; on his personal bot he prefers 'Samuel.'" One persona per (bot, user) tuple in v1.
- **Persona-per-correspondent.** "Always use 'Sam Riley' for vendor X; use 'Mr. Riley' for vendor Y." State-management problem; deferred.
- **Auto-discovery of user names from turn history.** Could pre-fill the alias editor's per-user `name` field from the resolved messaging-platform display name. Useful, but not required to ship C.

---

## 10. Open questions

1. **What about cron-triggered turns?** A cron-style background bot runs unattended; there's no initiating user. For multi-user bots, cron turns are vanishingly rare in practice (multi-user bots are interactive). `fallback` covers it. Worth being explicit in the spec.
2. **How does this interact with the in-progress evo account-separation work?** Evo runs as its own macOS user with no per-user concept beyond `pod.admins`. If evo ever grows a `correspondence` block, it'll be `static`. Not a blocker.
3. **Reply threading.** If the bot sends as Sam, then a reply arrives — does the bot reply *also* as Sam? Almost certainly yes; the reply path should resolve initiating-user from the *original* message's persona, not the bot's current session. Worth covering in C.2.
