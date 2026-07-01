# Spec: surface-aware help-style framework (2026-05-22)

**Status:** draft, locked-in design
**Authors:** synthesized from PRs [#1437](https://github.com/evolve-ops/evolve/pull/1437),
[#1438](https://github.com/evolve-ops/evolve/pull/1438),
[#1421](https://github.com/evolve-ops/evolve/pull/1421) /
[#1428](https://github.com/evolve-ops/evolve/pull/1428),
[#1430](https://github.com/evolve-ops/evolve/pull/1430) /
[#1431](https://github.com/evolve-ops/evolve/pull/1431)
**Implementation:** see §8 — six phases, this PR is docs-only

---

## 1. Principles + escalation hierarchy

### 1.1 The load-bearing principle

> *"In general, I think it is always better for evo to 'just do it' if it
> can. Don't make the user run a CLI if it doesn't have to. And don't
> make them press a button on the UI, if it doesn't have to. Evo should
> do as much work as it can, within the basic permission framework. But
> it needs to know to never give CLI in a mobile context, and to try and
> avoid in a web UI context. And in Telegram, it may be easier to use
> CLI than a UI button, but this is not hard and fast. Some Telegram
> users may much prefer 'tell me which button to press in the UI' than
> 'run this CLI'. Some people are uncomfortable with CLI."*
>
> — pod admin, 2026-05-22

The principle unpacks into three orthogonal axes:

1. **Capability axis** — what evo *can* do (registered tools + authority
   tier + operator confirmation). The escalation hierarchy.
2. **Surface axis** — what the operator can *use* given where they're
   reading (Telegram terminal, admin-UI laptop browser, admin-UI mobile
   browser, future surfaces).
3. **Preference axis** — what the operator *wants*, regardless of
   surface. Some Telegram operators prefer UI; some admin-UI laptop
   operators prefer CLI.

These three axes are independent. Surface tells you what's possible;
preference tells you what's wanted; capability tells you whether evo
can skip the operator entirely. The framework's job is to pick the
right rung of the escalation hierarchy from those three inputs.

Plus one **universal accuracy rule** that applies regardless of
surface or preference: hallucinated CLI is worse than no CLI in every
surface, because chasing a fabricated command costs operator trust on
top of the wasted time.

### 1.2 The four-rung escalation hierarchy

When the operator expresses an intent (*"raise team-bot-a's TTL"*, *"restart
the team-bot-b gateway"*, *"apply that cron-cap proposal"*), evo picks the
lowest-friction rung that's available and authorized:

| Rung | What it looks like | When it's reachable |
|------|--------------------|---------------------|
| **1. Evo just does it** | Tool call (with confirmation if `authority == "ask"` and the tool isn't `write_safe`). Conversational reply summarizing the result. | A registered `action.*` tool covers the intent AND the authority tier allows it AND the operator hasn't asked for a different style. |
| **2. UI button guidance** | *"Click **Apply on Cost Optimization →** on the recommendation card."* Names a specific page + button on the operator's current page or on a nearby page reachable from their sidebar. | No tool covers the intent (or the authority gate blocks it) AND the operator is on a surface with UI affordances AND a UI alternative for this intent exists. |
| **3. CLI guidance** | *"From the admin terminal: `sudo /bin/launchctl kickstart -k system/ai.openclaw.team-bot-a-gateway`."* Surface-conditional and accuracy-gated. | No tool, no UI alternative (or operator preference is CLI) AND the surface allows CLI AND the command is accuracy-verified. |
| **4. Tool-gap log** | *"I don't have a way to do this for you yet. I've logged it as a tool gap so it gets prioritized."* Calls `action.evo.log_tool_gap`. | Nothing else works. The intent stays visible to the dev process. |

### 1.3 Decision tree

For a given intent, evo's reasoning runs top-down. The first `yes`
wins; everything below it is skipped.

```
                    ┌─ Yes → Confirm (if authority gate)? → Call tool → Rung 1
1. Registered tool ──┤
   covers intent?    └─ No ─┐
                            │
                    ┌─ Yes ─┴→ Surface allows UI guidance?
2. Has UI alt?      │           ┌─ Yes → "Click X on Y page" → Rung 2
                    └─ No ──┐   └─ No ──┐
                            │           │
                    ┌─ Yes ──┴───────────┴→ Surface allows CLI? +
3. Has CLI shape?   │                       accuracy verified?
                    │                       ┌─ Yes → CLI w/ surface framing → Rung 3
                    │                       └─ No ──┐
                    └─ No ──────────────────────────┤
                                                    │
4. Fallback         └────────────────────────────────→ action.evo.log_tool_gap → Rung 4
```

The gates each rung tests, with the source of truth for each:

| Gate | Source of truth |
|------|-----------------|
| Tool exists | `meta.tools` (the registered-tool registry evo reads at session start) |
| Authority allows direct call | `<session-context>` `authority` field (`ask` / `auto-small` / `auto`) + the tool's own write-tier classification |
| Surface allows UI guidance | `<page-context>` exists (`surface == "admin_ui"`), OR Telegram with the operator's preference != `"cli"` |
| UI alternative exists for this intent | The UI-alternative lookup map (§7) — keyed on "intent" or "command-pattern", returns page + button |
| Surface allows CLI | Per §2 table (default), overridable by operator preference (§3) |
| CLI is accuracy-verified | Per §4 — bot_id resolution, path validity, command whitelist, schema-safe patterns |

### 1.4 Worked example

Operator on admin-UI laptop on the Recommendations page asks: *"raise team-bot-a's pruning TTL to 12h."*

1. **Rung 1?** Is there a tool that mutates `contextPruning.ttl`?
   - Today: **no**. `UpdatePermissionConfigApplier._ALLOWED_FIELDS` only
     covers permission fields (see [`permissions.py:38-50`](packages/analyzer/arbiter/appliers/permissions.py:38)).
   - After §6a lands: **yes**. `action.bot.update_behavior_config(bot_id="team-bot-a", fields={"contextPruning.ttl": "12h"})`
     → reaches Rung 1 directly.
2. **Rung 2?** Is there a UI alternative? **Yes** — the Cost Measures
   page's `cm-prune-ttl` selector at [`web/index.html:28427`](packages/admin/evolve_admin/web/index.html:28427)
   + the *"Apply on Cost Optimization →"* deep-link button on the TTL
   recommendation card at [`web/index.html:16871`](packages/admin/evolve_admin/web/index.html:16871).
   → *"Click the **Apply on Cost Optimization →** button on this card."*
3. **Rung 3?** Skipped — Rung 2 covers the intent.
4. **Rung 4?** Skipped.

Same operator on Telegram (no `<page-context>` block) with `help_style_preference == "either"`:

1. **Rung 1?** Same as above.
2. **Rung 2?** UI guidance applies even on Telegram (operator may have
   the admin UI open on another device). The model offers the UI route
   first, but on Telegram with `preference == "cli"` or `"either"` it
   may follow up with a CLI alternative.
3. **Rung 3?** If operator says *"give me the CLI"*: emit accuracy-
   verified CLI for the team-bot-a TTL change. After §6b lands, this is a
   templated invocation of `action.bot.exec_canonical(command_kind="update_behavior_config", bot_id="team-bot-a", args={...})`
   — the model emits the intent, the proxy renders the literal CLI
   using `bot_home("team-bot-a")`.
4. **Rung 4?** Skipped.

Same operator on admin-UI **mobile** asks the same thing:

1. **Rung 1?** Same. If `action.bot.update_behavior_config` lands first
   → Rung 1.
2. **Rung 2?** UI alternative exists; the Cost Measures page works on
   mobile too. → *"Click **Apply on Cost Optimization →** — it'll
   navigate you to the field on this device."*
3. **Rung 3?** **Never reached on mobile.** Even if Rung 1 + 2 both
   fail, CLI is never emitted on mobile.
4. **Rung 4?** *"I don't have a tool for this yet and the UI affordance
   I'd point you to isn't ideal on mobile. I've logged the gap."*

---

## 2. Surface taxonomy

### 2.1 Surfaces

| Surface | `page_context.surface` | `page_context.surface_type` | CLI policy (default) | UI guidance applies? |
|---|---|---|---|---|
| **Telegram** | (no `<page-context>` block — caller omits) | n/a | Allowed if accuracy-verified AND operator preference != `"ui"` | Yes (operator may have the admin UI open on another device) |
| **Admin UI — laptop chat or drawer** | `"admin_ui"` | `"laptop"` | Avoid; allowed only when no tool + no UI alternative AND operator preference != `"ui"`. Framed as *"switch to your admin terminal"*. | Yes — buttons are right there |
| **Admin UI — mobile chat or drawer** | `"admin_ui"` | `"mobile"` | **Never.** Emit Rung 4 instead. | Yes (buttons may be smaller but reachable) |
| **Future — IDE / VS Code panel** | `"ide"` | `"vscode"` etc. | Defined per surface when added | Per surface |
| **Future — Slack / Discord** | `"slack"` / `"discord"` | n/a | Same posture as Telegram | Yes (operator may have UI on another device) |

Only **Telegram**, **admin-UI laptop**, and **admin-UI mobile** are in
scope for v1. The "future" rows are placeholders showing the taxonomy
extends — they're not implemented.

### 2.2 Detection mechanism — client-side `surface_type` plumb-through

The client (browser) computes `surface_type` and includes it in
`page_context`. Detection is done client-side because:

- The browser knows its own viewport (`window.matchMedia`).
- The server has no reliable way to infer mobile-vs-laptop from
  User-Agent alone (modern UA strings are unreliable; "iPad in desktop
  mode" + "laptop in mobile-emulator" both fool UA sniffing).
- The proxy is a thin pass-through; pushing the detection to the edge
  (the browser) keeps the proxy logic simple.

Server-side User-Agent inspection is a fallback only — if `page_context`
arrives without `surface_type`, the route handler may set it from a
User-Agent classifier as a defensive default (favor `"laptop"`, since
emitting CLI to a misclassified mobile is the failure mode we're
trying to avoid).

### 2.3 Concrete plumbing

The detection is a small addition to the existing page-context pack
machinery at [`web/index.html`](packages/admin/evolve_admin/web/index.html:32655).

**Step 2.3.1 — `_evoDrawerContextPack` adds `surface_type`:**

```js
// packages/admin/evolve_admin/web/index.html, near line 32655
function _evoDrawerContextPack() {
  const page_id = _evoDrawerCurrentPage();
  const pack = {
    page_id,
    page_label: _evoDrawerPageLabel(page_id),
    surface: 'admin_ui',                       // already implicit; make explicit
    surface_type: _evoSurfaceType(),           // new — see 2.3.2
  };
  // ... existing summary builder logic
  return pack;
}

// New helper, defined near the other context-pack helpers.
function _evoSurfaceType() {
  // Two-tier classifier:
  //   - `mobile`  — viewport <= 720px wide, OR pointer:coarse (touch-primary).
  //   - `laptop`  — otherwise (desktop + iPad-in-desktop-mode + tablets).
  // The conservative bias is intentional: when in doubt, classify
  // as `laptop` so CLI emission stays gated by accuracy verification,
  // not by surface-type alone. Mobile is the strict-block case;
  // mis-detecting a real mobile as laptop is the failure we want to
  // avoid.
  try {
    const narrowMQ = window.matchMedia('(max-width: 720px)');
    const coarseMQ = window.matchMedia('(pointer: coarse)');
    if (narrowMQ.matches || coarseMQ.matches) return 'mobile';
  } catch (_) {}
  return 'laptop';
}
```

The same pack is also used by the Chat home page's sender at
[`web/index.html:33283`](packages/admin/evolve_admin/web/index.html:33283),
so this single addition covers both chat surfaces.

**Step 2.3.2 — route handler plumbs `surface_type` through:**

[`home_chat_routes.py:111`](packages/admin/evolve_admin/web/home_chat_routes.py:111)
reads `page_context` from the request body. No change needed there —
`surface_type` flows through unchanged because the proxy reads it from
the same dict.

**Step 2.3.3 — `format_page_context` includes `surface_type` in the
XML block:**

[`proxy.py:562`](packages/admin/evolve_admin/evo/proxy.py:562) already
extracts `surface` from `page_context` at line 610 and emits it as the
first attribute on the `<page-context>` tag (line 622). Extend the
same shape:

```python
# packages/admin/evolve_admin/evo/proxy.py, around line 605-625
def format_page_context(page_context: dict[str, Any] | None) -> str:
    # ... existing extraction
    surface = (page_context.get("surface") or "admin_ui").strip()
    surface_type = (page_context.get("surface_type") or "").strip()
    # ...

    attrs.append(f'surface="{_xml_quote(surface)}"')
    if surface_type:
        attrs.append(f'surface_type="{_xml_quote(surface_type)}"')
    # ... rest unchanged
```

**Step 2.3.4 — `format_session_context` adds a one-line surface
echo:**

The session-context block ([`proxy.py:226`](packages/admin/evolve_admin/evo/proxy.py:226))
is where the model reads "who am I talking to and where." Add a
**single line** that the model can use even when the `<page-context>`
block is absent (Telegram) or when the operator's preference flips
the surface logic:

```python
# packages/admin/evolve_admin/evo/proxy.py, in format_session_context
# (between the operator line and the local_time line, ~line 277)
lines.append(f"Operator: {operator_id} (authority tier: {authority})")
if surface or surface_type:
    surface_label = surface or "(unknown)"
    if surface_type:
        surface_label += f" / {surface_type}"
    lines.append(f"Surface: {surface_label}")
if help_style_preference and help_style_preference != "either":
    lines.append(f"Help style preference: {help_style_preference}")
```

The model sees one extra line of session context: `Surface: admin_ui / mobile`.
On Telegram, the line reads `Surface: telegram` (set explicitly by the
Telegram dispatcher; see §2.4). When `help_style_preference == "either"`
the preference line is omitted to avoid prompt noise.

### 2.4 Telegram dispatcher emits `Surface: telegram`

The Telegram dispatcher already calls `send_to_evo` for chat
turns; it just doesn't supply a `page_context` block today (per
[`AGENTS.md:147`](packages/analyzer/evolve_bot/AGENTS.md:147) the
absence of the block IS the Telegram signal). For the new framework,
the dispatcher passes a minimal `session_context` overlay so the
session-context block carries an explicit `Surface: telegram` line.
Concretely, in the Telegram caller's invocation of `send_to_evo`:

```python
session_ctx = {
    **base_session_ctx,
    "surface": "telegram",
    # surface_type intentionally omitted — Telegram has no viewport
}
```

The model now has a single, authoritative source of truth for the
operator's current surface in every code path: the session-context
block. The `<page-context>` block remains the secondary signal for
admin-UI specifics, but the surface decision is anchored above it.

---

## 3. Operator preference axis

### 3.1 Why preference is orthogonal to surface

Surface tells you *what's possible*. Preference tells you *what's
wanted*. The two interact but don't determine each other:

- A Telegram operator with `preference == "ui"` should hear *"go to
  the Cost Measures page on the admin UI"* even though Telegram could
  carry CLI fine — they prefer not to use CLI.
- A laptop admin-UI operator with `preference == "cli"` should get
  CLI in addition to (or instead of) the UI button, because they
  prefer the keyboard route.
- An admin-UI **mobile** operator with `preference == "cli"` should
  **still not get CLI** — surface is the hard ceiling, preference is
  a tie-breaker below the ceiling.

The framework resolves this with two complementary mechanisms.

### 3.2 Persistent setting

Stored in `network.json` under a new `operators` section:

```json
{
  "pod": { ... },
  "bots": [ ... ],
  "operators": {
    "pod_admin": {
      "help_style_preference": "either"
    }
  }
}
```

**Schema:**

| Field | Type | Default | Notes |
|---|---|---|---|
| `operators.<id>.help_style_preference` | `"ui" \| "cli" \| "either"` | `"either"` | Resolution order: see §3.4 |
| `operators.<id>.display_name` | string | empty | Future; carried for parity with the existing implicit `pod_admin` identity |

For v1 there is exactly one operator identity (`pod_admin`); the
section is keyed by operator-id to scale to multi-operator pods
without a re-design. New entries default to `"either"` so existing
behavior is unchanged for any operator who hasn't opted in.

**Storage location.** `network.json` is the only source of truth for
pod-wide explicit config (per
[`feedback_explicit_pod_membership`](memory:feedback_explicit_pod_membership)).
Per-operator preference is pod-wide config, so this is the natural
home. A per-operator sidecar (e.g., `operators/<id>.json`) is the
future-scale move — deferred until there's a second operator.

**Where it's set:**

- **Admin UI** (canonical surface): Settings → Pod Config → Identity →
  *Help style preference* radio (3 options: UI, CLI, Either). The
  Settings page already owns network.json identity writes;
  `help_style_preference` joins the existing identity-card fields.
- **Telegram** (low priority): `evo set preference ui` keyword
  command, handled by the same code path that handles other `evo set
  ...` commands. Not load-bearing — the Settings page is the canonical
  surface — but adds the affordance for operators who only ever use
  Telegram.

**Migration:** none. The field is absent for any existing pod and
defaults to `"either"`, which preserves the v1.5 surface-only default
behavior.

### 3.3 In-conversation expression

The operator can say *"tell me what button to press in the UI"* or
*"just give me the CLI for this"* at any point. Evo respects the
expressed preference for the rest of the thread.

This is detected by the model itself, not by a pattern match in the
proxy. Phrases vary (*"give me the click path"*, *"I'd rather paste a
command"*, *"can you walk me through the UI?"*), and the model's
natural-language understanding handles the variance robustly. An
AGENTS.md rule (§5d below) names the behavior:

> **Hard rule — honor in-thread preference expressions.** If the
> operator says they want UI guidance, CLI guidance, or a specific
> style for the rest of the thread, treat that as overriding the
> default for this thread. Don't keep emitting CLI after they asked
> for UI clicks.

The expression lives in the thread's turn history (where it's already
visible to the model on every subsequent turn — that's what
`<session-context>` `user_turn_count` makes legible per
[`proxy.py:289-307`](packages/admin/evolve_admin/evo/proxy.py:289)).
It does NOT update the persistent setting unless the operator
explicitly says *"and remember that preference"* — the in-thread
expression is per-thread, not pod-wide.

### 3.4 Resolution order

When evo picks the right rung:

1. **In-conversation expression** (if any) — overrides everything else
   for the current thread.
2. **Persistent `help_style_preference`** (if not `"either"`) —
   overrides surface default.
3. **Surface default** (per §2.1 table) — the baseline.

Examples:

| Surface | Persistent | In-thread expression | Resolved style |
|---|---|---|---|
| admin_ui / laptop | `either` | none | UI guidance preferred; CLI allowed as fallback when no UI alt |
| admin_ui / laptop | `cli` | none | CLI guidance with full accuracy gating |
| admin_ui / laptop | `ui` | none | UI guidance only; falls through to Rung 4 if no UI alt |
| admin_ui / mobile | `either` or `cli` | none | UI guidance only; CLI **never** emitted; Rung 4 if no UI alt |
| admin_ui / mobile | `cli` | none | Same as above — **surface ceiling overrides preference** |
| telegram | `either` | none | CLI allowed; UI guidance also fine if helpful |
| telegram | `ui` | none | UI guidance, even though we're on Telegram |
| telegram | `cli` | *"actually walk me through the UI for this one"* | UI guidance for this thread |

### 3.5 Plumbing the persistent setting

**Step 3.5.1 — `home_chat_routes.py` reads the operator's preference
from network.json:**

```python
# packages/admin/evolve_admin/web/home_chat_routes.py, around line 209
# After computing operator_id (v1: always "pod_admin"):
from ..config import load_network
network = load_network(network_path)
operator_record = (network.get("operators") or {}).get(operator_id) or {}
help_style_pref = operator_record.get("help_style_preference") or "either"

session_ctx = {
    "operator_id": operator_id,
    "authority": authority,
    "local_time": (body.get("local_time") or "").strip(),
    "session_age_seconds": stats["age_seconds"],
    "user_turn_count": stats["user_turn_count"],
    "help_style_preference": help_style_pref,   # new
    "surface": "admin_ui",                       # echoed for the session-context block
}
```

The Telegram dispatcher does the equivalent — reads the operator's
preference once at dispatcher-init and passes it on every turn.

**Step 3.5.2 — `format_session_context` already renders it** (per the
§2.3.4 patch above). The model sees `Help style preference: ui`
(or `cli`) when the operator has set a non-default; nothing when
they're at `"either"`.

**Step 3.5.3 — Settings-page UI writes the field via the existing
network.json save path:**

The Settings page already POSTs partial network.json updates via the
existing identity-save handler. The new field slots into the same
schema-validated path with no new endpoint. UI shape:

```
Help style preference
  ( ) Click UI buttons         — "I'd rather click than type commands."
  ( ) Run CLI commands         — "I'm comfortable in a terminal; prefer CLI."
  (•) Either is fine           — Default. Evo picks based on context.
```

### 3.6 Plex-test note

Per [`feedback_design_constraint_mildly_tech_capable`](memory:feedback_design_constraint_mildly_tech_capable),
the default operator is someone who can install Plex but doesn't
necessarily know what `sudo` means. **`"either"` is the right default
specifically because surface-aware reasoning then prefers UI on the
admin UI** — the Plex-target operator gets UI clicks without having
to think about preferences. The `"cli"` value is opt-in for power
users; the `"ui"` value is the opt-in lockdown for operators who
explicitly want to be insulated from CLI even on Telegram.

---

## 4. Universal accuracy rule

**Hallucinated CLI is always worse than no CLI.** Regardless of
surface, regardless of preference, if evo is about to emit a CLI
command, it must be one that's verified accurate before it exits the
proxy.

The damage audit in PR #1438 found 4/8 actionable shell snippets
would have failed in non-obvious ways, including:

- **Schema-coupled `sed` patterns** — `s/"ttl": "4h"/"ttl": "12h"/`
  matches *every* `"ttl": "4h"` in `openclaw.json`. Works today by
  accident; silently clobbers future fields when more `"ttl": "4h"`
  values exist.
- **bot_id ≠ account_name** — `sudo -u team-bot-b … /Users/team-bot-b/…` fails
  because team-bot-b runs on the `personal-bot-user` account
  (per [`feedback_bot_id_not_account_name`](memory:feedback_bot_id_not_account_name)).
  The snippet looks plausible to the operator but breaks with
  `unknown user: team-bot-b`.

### 4.1 Verification table

Each verification rule has a specific check, location, and outcome.

| Verification | Check mechanism | Where it runs | On failure |
|---|---|---|---|
| **bot_id resolution** | Any `sudo -u <bot>` or `/Users/<bot>/…` reference must resolve via `bot_home()` / `get_bot_user()` in `evolve_config.py`. The inspector pre-resolves each `<bot>` token and compares against the literal in the snippet. | Inspector regex (cheap) — flags any `sudo -u <id>` or `/Users/<id>/.openclaw` token; the haiku confirmer cross-checks against `bot_home(<id>)`. | **Rewrite** the snippet to use the resolved path. If resolution fails, **reject** with a Rung 4 substitution. |
| **Path validity** | Canonical paths (`/Users/<account>/.openclaw/openclaw.json`, `/Users/Shared/evolve/…`) are a known set. The inspector checks any `/Users/.../…` token against the canonical set. | Inspector after bot_id resolution. | **Reject** (substitute). Log a tool-gap so the unknown-path pattern surfaces. |
| **Command whitelist** | The legacy Command Reference at [`AGENTS.md:912-1136`](packages/analyzer/evolve_bot/AGENTS.md:912) defines the supported operator-facing CLI surface. Any command outside it is "ad-hoc" — allowed only with a `verify before running` caveat AND only on Telegram. | Inspector regex extracts the command verb; cross-checks against the whitelist. | **Pass with caveat** if on Telegram; **substitute** with Rung 2 or Rung 4 otherwise. |
| **Schema-safe edits** | No `sed`, `awk`, or text-mutation tool on JSON files. JSON edits must go through a registered tool (Rung 1) or a templated CLI from structured intent (§6b). | Inspector — `\b(sed|awk)\b.*\.json` is a hard reject pattern. | **Reject** unconditionally; offer Rung 1 if `action.bot.update_behavior_config` covers the field, else Rung 4. |
| **Idempotency** | Mutating operations should be naturally idempotent or have a recovery clause. Tracked but not enforced in v1 — emitted as an inspector telemetry tag (`non_idempotent: true`) for review. | Inspector telemetry only. | (no rewrite; advisory) |

### 4.2 Inspector composition

The verification stages compose in the inspector (full design in §7):

```
extract_cli_blocks(text)
  → bot_id_resolve()        # rewrite known patterns
  → path_validity()         # reject unknown paths
  → schema_safety()         # reject sed/awk on JSON
  → command_whitelist()     # tag known vs ad-hoc
  → surface_policy(surface, surface_type, preference)
      # final decision: pass / rewrite / substitute
```

Each stage is independently testable and the rejection/rewrite reason
is logged with the substituted text so the operator sees a useful
substitution message and the diagnostics layer captures *what kind*
of failure the model produced.

### 4.3 What the operator sees on a verification failure

When a snippet is rewritten (bot_id resolution succeeds, path now
correct):

> *(the original snippet, with `/Users/team-bot-b/` rewritten to `/Users/personal-bot-user/`,
> and a one-line note appended:)*
> `(I corrected the path — team-bot-b runs on the personal-bot-user account.)`

When a snippet is rejected (path or schema-safety failure):

> *"I started to recommend a shell command but couldn't verify it
> safely. Let me look up the right registered tool or UI surface
> instead — I've logged the gap."*

Substituted-snippet cases always log to the diagnostics inspector
trail so the dev team can see (a) what intent the model had and (b)
why verification rejected.

---

## 5. AGENTS.md restructure

Concrete edits to
[`packages/analyzer/evolve_bot/AGENTS.md`](packages/analyzer/evolve_bot/AGENTS.md).
The current state has three legacy sections that contradict the
modern shell-snippet rule (see PR #1438 cause analysis); the surgery
keeps them as Telegram-conditioned reference material rather than
deleting them outright.

### 5a. Add "Help style decision tree" section (after line 128)

A new top-level section, inserted right after the existing **Surface
awareness** (lines 106-128) so the decision tree is the first thing
the model reads after surface framing.

```markdown
---

## Help style — the four-rung escalation

For every operator intent, pick the lowest-friction rung that's
reachable and authorized:

1. **Rung 1 — Just do it.** A registered `action.*` tool covers the
   intent. Call it (with confirmation if authority gate requires).
   Reply with a one-line summary of what you did.
2. **Rung 2 — Walk to the UI button.** No tool fits, but a UI
   affordance does. Name the exact page + button — never invent
   navigation paths.
3. **Rung 3 — CLI guidance.** No tool, no UI alternative, surface
   allows CLI. Emit a verified-accurate command framed for the
   operator's surface.
4. **Rung 4 — Log a tool gap.** Nothing else works. Call
   `action.evo.log_tool_gap(intent="...")` and tell the operator
   what's missing.

The session-context block tells you the operator's surface and
their preference. Use them:

- Surface in `<session-context>`: `Surface: admin_ui / laptop` /
  `admin_ui / mobile` / `telegram`. If the line is absent, infer
  from whether a `<page-context>` block is present.
- Preference (when set): `Help style preference: ui` /
  `cli`. Absent means `either` — let surface decide.

Surface defaults (when preference is `either`):

| Surface | Default style |
|---|---|
| admin_ui / mobile | UI guidance only; never CLI |
| admin_ui / laptop | UI guidance preferred; CLI as last resort with terminal framing |
| telegram | CLI allowed; UI guidance also helpful (operator may have admin UI on another device) |

When the operator expresses an in-thread preference (*"give me the
CLI"*, *"walk me through the UI"*), honor it for the rest of the
thread regardless of the persistent setting.
```

### 5b. Conditionalize the legacy Command Reference (lines 912-1136)

The existing **Command Reference** section was written for the
Telegram-only era and lists `sudo /bin/launchctl kickstart`,
`python3 /Users/Shared/evolve/packages/analyzer/audit.py …`, etc., as
the canonical evo operator workflow. With surface awareness, the same
section is correct **on Telegram** and wrong on admin-UI surfaces.

Prefix the section header (around line 910) with a surface gate:

```markdown
---

## Command Reference

> **Surface-conditioned.** The commands below are vetted CLI workflows
> for **Telegram operators**. On admin-UI surfaces (`Surface: admin_ui
> …`) — DO NOT emit these commands as text:
>
> - First try a registered `action.*` tool (Rung 1) — `meta.tools`
>   lists what's available. The Common operations map below names
>   tool/UI mappings for the verbs in this Command Reference.
> - If no tool fits, walk the operator to the UI button (Rung 2) per
>   the Common operations map.
> - CLI on admin-UI is a last resort, allowed only on laptop surfaces
>   when no tool + no UI alternative exists. **Never on mobile.**
>
> On Telegram, these commands are still the correct operator-facing
> reference. Honor `Help style preference: ui` even on Telegram by
> preferring UI guidance when one exists.

### Pod Status
...
```

The body content stays unchanged — the model still needs the
canonical command shapes for accuracy verification (§4) and for
Telegram emission.

### 5c. Conditionalize the Privilege Boundary (lines 1169-1186)

Same surgery. The section opens with:

> **You CAN do directly (as evolve user):** … Restart gateways via
> `sudo /bin/launchctl kickstart` …

That's load-bearing context for Telegram but reads as
"justify-emitting-shell" framing on admin-UI. Add a gate at the top:

```markdown
---

## Privilege Boundary

> **Surface-conditioned.** This section describes what evo, as the
> `evolve` Unix user, can do at the OS layer. It's reference for
> *what the underlying system supports* — not a checklist of things
> evo should emit as chat-text shell snippets on the admin UI.
>
> On admin-UI surfaces, prefer a registered tool that wraps the OS
> action (`action.bot.restart` for gateway kickstart, etc.).
> CLI emission on admin-UI is governed by the help-style rules
> above; the bullet list below is the underlying capability map,
> not the recommendation map.

**You CAN do directly (as evolve user):**
...
```

The bullet content (CAN / CANNOT) stays unchanged; it's referenced
elsewhere for the same accuracy-verification purpose.

### 5d. Rewrite the never-propose-shell rule (line 234)

The current rule's lede ("**never** invent a `sudo` … shell
incantation") reads as "don't fabricate" — but in the team-bot-a/team-bot-b case
the model was describing real commands, not invented ones. The
rewritten rule leads with the **outcome** (no shell in front of the
operator) and adds surface conditioning:

```markdown
**Hard rule (surface-conditional): shell snippets.**

The decision of whether to put a shell command in front of the
operator depends on their surface, their preference, and the
command's accuracy:

- **On `Surface: admin_ui / mobile`**: NEVER emit a shell command.
  The operator can't run it. Always use a registered tool (Rung 1),
  UI button guidance (Rung 2), or `action.evo.log_tool_gap` (Rung 4).

- **On `Surface: admin_ui / laptop`**: Avoid shell commands. Prefer
  Rung 1, then Rung 2. Shell is allowed only when neither alternative
  exists AND the operator hasn't expressed `Help style preference: ui`.
  When you do emit shell, frame it as *"from your admin terminal:"*
  with explicit acknowledgment that the operator has to switch
  surfaces to run it.

- **On `Surface: telegram`**: Shell is welcome IF the operator's
  preference allows AND the command is accuracy-verified (see below).
  This is your primary use case — Telegram operators have a terminal
  nearby and CLI is often the fastest path.

- **Universal accuracy floor (every surface):** Never propose a shell
  command you can't verify:
  - `sudo -u <bot>` / `/Users/<bot>/…` paths must resolve via the
    bot_id → account_name mapping (`team-bot-b` runs on `personal-bot-user`; the
    inspector rewrites or rejects mismatches).
  - No `sed`, `awk`, or text-mutation on JSON files. Schema-coupled
    patterns silently corrupt future configs.
  - Commands outside the Command Reference (above) are "ad-hoc" —
    emit with a `# verify before running` caveat, never on the
    admin UI, and prefer a registered tool when one exists.

If you catch yourself writing `sudo`, `python3 -c`, `launchctl`,
`sed -i`, `chmod`, `chown`, or `find /Users/…` on an admin-UI
surface, STOP. Apply the decision tree above; one of the four rungs
covers it.

The "shell exec permission tier" framing — *"I'd need elevated exec
access"*, *"exec is denied in my context"* — is fabricated capability
language. Don't reach for it. The right shape is one of:
- *"The tool for this is X"* (Rung 1)
- *"Click X on Y page"* (Rung 2)
- *"From your admin terminal: …"* (Rung 3, with surface + accuracy gates)
- *"I don't have a way to do that yet — logging the gap."* (Rung 4)
```

### 5e. Admin-UI closing-summary carve-out (new — from PR #1437)

Add after the new shell-snippet rule:

```markdown
**Hard rule — admin-UI closing summary after a tool batch.**

On `Surface: admin_ui / *` after a turn that emits one or more tool
calls (`stop_reason: tool_use`), ALWAYS produce a closing text turn
summarizing the result. Never let a turn end on the tool batch
itself — the proxy may not surface intermediate text to the
operator and the chat bubble will read as an empty reply
(see PR #1437 — the empty-reply-after-successful-tool-calls diagnosis).

The closing summary should be one line per non-trivial tool call,
naming what changed (eg *"applied 7 cron-cap proposals; 2 failed
on file-lock contention"*). On Telegram the legacy "never narrate
what you just did" rule still applies — admin-UI is the carve-out.
```

### 5f. Common operations table — add Cost Measures row

At the **Common operations → admin UI surface** table (around line 351):

```markdown
| Change a bot's context-pruning TTL, compaction mode, idle-reset
threshold, or other behavior-config field | Cost Optimization
page → bot section → field editor (or `action.bot.update_behavior_config`
tool once available). The TTL recommendation card on Recommendations
has an *"Apply on Cost Optimization →"* deep-link button that
pre-selects the bot and highlights the field. |
```

### 5g. Test suite update

The `test_never_propose_shell_snippets_rule_present` guard at
[`tests/test_evo_agents_md_guards.py:108`](packages/admin/tests/test_evo_agents_md_guards.py:108)
currently pins the forbidden tokens (`sudo`, `python -c`,
`launchctl`). Extend the test:

- Pin the new rule by its **section title** (`Hard rule (surface-conditional): shell snippets`),
  not by the forbidden-token list alone — the section now has more
  nuance than a single regex can capture.
- Add a new guard `test_help_style_decision_tree_present` that
  checks the four-rung escalation is named in AGENTS.md.
- Add a guard `test_surface_conditioned_command_reference` that
  checks the Command Reference + Privilege Boundary sections both
  have the "Surface-conditioned." preface block.

---

## 6. Tool surface expansion (Rung-1 reach)

Closing the missing-tool gap is the highest-leverage move — every
new tool converts a "currently needs CLI" workflow into Rung 1.

### 6a. `action.bot.update_behavior_config`

Closes the team-bot-a/team-bot-b TTL gap directly (the original failure that
triggered this spec). Signature:

```python
def update_behavior_config(
    bot_id: str,
    fields: dict[str, Any],
    *,
    reason: str | None = None,
) -> ToolResult:
    """Update whitelisted behavior-config fields on a bot's openclaw.json.

    Whitelist (initial):
      - agents.defaults.contextPruning.ttl
      - agents.defaults.contextPruning.maxAgeMinutes
      - agents.defaults.compaction.mode
      - agents.defaults.compaction.reserveTokensFloor
      - agents.defaults.compaction.triggerThreshold
      - agents.defaults.session.reset.idleMinutes
      - agents.defaults.session.reset.maxTurns

    NOT included (use UpdatePermissionConfig instead):
      - tools.exec.*
      - commands.*
      - tools.fs.*
      - tools.web.*
    """
```

**Implementation:**

- New module `packages/analyzer/arbiter/appliers/behavior_config.py`,
  parallel to `permissions.py` but with the behavior-config whitelist
  instead of the permission whitelist. Both share the same
  `permissions/writer.py::write_openclaw_fields` underlying writer
  (the L2 pattern from PR #1431).
- Field whitelist enforced at the applier boundary (refuse anything
  outside the set with a clear error).
- After the write, calls the same gateway-kickstart step the
  permission applier uses.
- Records intent via the config-intent system (per
  [`feedback_generators_consider_intent`](memory:feedback_generators_consider_intent))
  when that lands — for now, no intent recording (the generator
  side hasn't been wired yet, so there's nothing to surprise).

**Tool definition** registered into evo's tool surface:

```json
{
  "name": "action.bot.update_behavior_config",
  "description": "Update whitelisted behavior-config fields on a bot's openclaw.json — context pruning, compaction, session reset.",
  "input_schema": {
    "bot_id": "string",
    "fields": { "object": "<dot-path>: <value> pairs" },
    "reason": "string (optional)"
  },
  "authority": "ask"   // never auto — config changes always confirm
}
```

**Test surface:** unit tests for the whitelist + writer round-trip
(modeled on [`tests/test_permissions_applier.py`](packages/admin/tests/test_permissions_applier.py));
integration test that exercises the L2 path on a temp openclaw.json.

**LOC + time:** ~200 LOC + ~½ day implementation; depends only on
the existing `permissions/writer.py` infrastructure (no new sudoers
grants, no new ACL changes).

### 6b. `action.bot.exec_canonical(command_kind, bot_id, args?)`

Closes the bot_id ≠ account_name hallucination class. Instead of the
model emitting *literal CLI strings*, it emits a structured **intent
+ arguments** and the proxy renders the verified-literal command.

**Signature:**

```python
def exec_canonical(
    command_kind: str,
    bot_id: str | None = None,
    args: dict[str, Any] | None = None,
    *,
    dry_run: bool = False,
) -> ToolResult:
    """Execute a vetted canonical command by kind.

    command_kind enumerates the Command Reference verbs in AGENTS.md:
      - "restart_gateway"    (requires bot_id)
      - "reset_baselines"    (no args)
      - "run_audit"          (args: {dry_run: bool})
      - "run_pod_report"     (args: {force: bool})
      - "ack_cve"            (args: {key: str})
      - "show_health"
      - "show_versions"
      - ... (one entry per Command Reference verb)

    Rendering rules:
      - bot_id always passes through bot_home() / get_bot_user()
        before being interpolated into a path or `sudo -u`
      - paths are interpolated from a canonical-paths registry,
        not from string templates
      - on dry_run=True, returns the literal command WITHOUT
        executing (used by the proxy to emit verified CLI to
        Telegram operators)
    """
```

**The structural win:** the model literally cannot hallucinate a bad
command because it's not typing the command — it's selecting from a
typed enum. The proxy uses `bot_home(bot_id)` for every
substitution, so the team-bot-b → personal-bot-user resolution is handled by
construction.

**On Telegram with `preference == "cli"`:** the model calls
`exec_canonical(command_kind="restart_gateway", bot_id="team-bot-a", dry_run=True)`,
the tool returns the literal command, and the model emits it in
chat. The operator sees `sudo /bin/launchctl kickstart -k system/ai.openclaw.team-bot-a-gateway`
— accurate by construction.

**On admin UI:** the model calls `exec_canonical(...)` without
`dry_run` (assuming authority allows), the tool executes, and the
model's closing summary reports the result. Same code path, two
output shapes, zero string templating in the model.

**Implementation:**

- New module `packages/analyzer/arbiter/exec_canonical.py`.
- A `_COMMAND_REGISTRY` dict mapping `command_kind` → handler. Each
  handler is a pure Python function that takes resolved args and
  returns either `(literal_cmd: list[str], output: str)` (on
  execute) or `literal_cmd` (on dry_run).
- The registry mirrors the Command Reference in AGENTS.md — one
  entry per verb. New verbs land via the same PR that adds the
  Command Reference entry (or vice versa).
- Audit log: every `exec_canonical` execution writes to
  `{shared_dir}/logs/exec_canonical.jsonl` with `command_kind`,
  args, resolved literal command, and result code.

**Test surface:** unit tests per `command_kind` that verify (a) the
literal rendering matches the Command Reference, (b) bot_id resolves
through `bot_home`, (c) `dry_run` doesn't execute, (d) unknown
command_kind rejects.

**LOC + time:** ~400 LOC for the initial ~20 commands + ~1 day.

### 6c. Future tool surface (placeholder)

The same pattern extends to other "currently requires admin
terminal" workflows. Each one converts a Rung 3 (CLI) workflow into
Rung 1 (just do it):

| Future tool | What it replaces |
|---|---|
| `action.network.update_operator_preferences` | Manual edits to `network.json::operators.<id>` from the admin terminal — alerts opt-out, help-style, etc. |
| `action.signals.update_config(signal_type, thresholds)` | Manual edits to signal-config files; tunes watchdog / cost / audit thresholds. |
| `action.bot.repair_backup_acls(bot_id)` | The hand-rolled `chmod +a` loops the audit caught (PR #1438 row 4). |
| `action.network.add_operator(id, channel_user_id)` | Manual `network.json::pod.admins` edits. |

These are not in scope for this spec; named here so each phase can
fold them in as transcripts surface the need. The framework's
expectation is that the Rung-1 surface expands over time and the
Rung-3 surface shrinks proportionally.

---

## 7. Surface-aware inspector

The inspector is the last-line defense — when teaching fails (the
five+ prior AGENTS.md tightenings haven't converged), the inspector
catches the model in the proxy choke point before the reply reaches
the operator.

### 7.1 Choke point

The inspector lives at the [`send_to_evo`](packages/admin/evolve_admin/evo/proxy.py:803)
return path — specifically, between `_parse_agent_json` extracting
`text` and the `ProxyResult` being constructed. Every admin-UI Chat
turn flows through this path; every Telegram turn likewise.

```python
# packages/admin/evolve_admin/evo/proxy.py, around line 915
text, model, usage, run_id = _parse_agent_json(proc.stdout)

# NEW: surface-aware inspector. Runs only when the proxy succeeded
# at the OC layer — empty text is handled by the empty-reply fallback
# below (Priority 1 from PR #1437).
if text:
    text = _inspect_outgoing_text(
        text,
        session_context=session_context,
        page_context=page_context,
    )

if not text:
    # ... existing empty-reply fallback
```

The inspector's job: take the model's text, return either the
original text (pass-through), a rewritten version (accuracy fix), or
a substituted version (rejection with operator-visible explanation).
It does NOT block; every input produces an output. Substitutions
emit a telemetry event so the dev team can see the failure pattern.

### 7.2 Decision logic

The inspector now runs two pre-filters in sequence — the regex
extracts CLI-shaped blocks (catches the shell-as-advice family), and
a haiku confirmer asks the multi-criterion question to catch the
two prose-shaped families (permission-tier fabrication, precondition
staleness) that the regex cannot see. Per the amendments in §7.4,
haiku is required in v1, not optional.

```python
def _inspect_outgoing_text(
    text: str,
    *,
    session_context: dict | None,
    page_context: dict | None,
) -> str:
    surface, surface_type, preference = _resolve_surface(
        session_context, page_context,
    )

    cli_blocks = _extract_cli_blocks(text)
    haiku_flag = _haiku_confirm(text)   # yes/a, yes/b, yes/c, no

    if not cli_blocks and haiku_flag == "no":
        return text   # no shell content, no fabrication, no staleness

    # PRECONDITION-STALENESS BRANCH (PR #1479) — runs first because
    # it can fire on responses that ALSO have shell content; safer to
    # re-verify than to substitute the shell and let the staleness
    # through.
    if haiku_flag == "yes/c":
        _log_inspector_event("precondition_staleness", haiku_flag.reason)
        return _SUBSTITUTION_STUBS["precondition_staleness"]

    # PERMISSION-TIER FABRICATION BRANCH (PR #1480) — strip preface
    # clause, keep constructive remainder.
    if haiku_flag == "yes/b":
        _log_inspector_event("permission_tier_fabrication", haiku_flag.reason)
        return _strip_permission_preface(text)

    # SHELL-RECOMMENDATION BRANCH — regex caught a CLI block AND
    # haiku confirmed it's a recommendation (yes/a). If haiku says
    # "no" despite a regex match, treat as prose mention and pass
    # through.
    if not cli_blocks or haiku_flag != "yes/a":
        return text

    # 1. Universal accuracy verification (runs regardless of surface)
    accuracy = _verify_cli_accuracy(cli_blocks)
    if accuracy.has_hallucination:
        _log_inspector_event("hallucinated_cli", accuracy.reason)
        return _substitute_with(
            "I started to recommend a shell command but couldn't "
            "verify it. Let me look up the right registered tool or "
            "UI surface instead."
        )

    # 2. Surface policy
    if surface == "admin_ui" and surface_type == "mobile":
        _log_inspector_event("cli_on_mobile")
        return _substitute_with(
            "This chat is on mobile — I can't recommend CLI commands "
            "you'd need to run from a terminal. "
            + (_ui_alternative_for(cli_blocks) or
               "I've logged this as a tool gap.")
        )

    if surface == "admin_ui" and surface_type == "laptop":
        ui_alt = _ui_alternative_for(cli_blocks)
        if ui_alt:
            _log_inspector_event("cli_on_laptop_with_ui_alternative")
            return _prepend_ui_guidance(text, ui_alt)
        if preference == "ui":
            _log_inspector_event("cli_on_laptop_ui_preferred")
            return _substitute_with(
                "I don't have a UI alternative for this one yet — "
                "logging the gap so it gets prioritized."
            )
        # laptop, no UI alt, preference != ui → allow with friction framing
        return _prepend_terminal_framing(text)

    if surface == "telegram":
        if preference == "ui":
            ui_alt = _ui_alternative_for(cli_blocks)
            if ui_alt:
                return _substitute_with_ui_guidance(ui_alt)
            # No UI alt; respect that and allow CLI even with ui preference
            return text
        # Default Telegram path: CLI welcome, already verified
        return text

    # Unknown surface — conservative: pass through with telemetry
    _log_inspector_event("unknown_surface", surface=surface)
    return text
```

### 7.3 The regex pre-filter

The cheap first stage is a regex pass that extracts CLI-shaped
content. Built from the audit findings + the legacy Command
Reference's command verbs:

```python
_CLI_BLOCK_PATTERNS = (
    re.compile(r"```(?:bash|sh|shell|console)\s*\n(.*?)\n```", re.DOTALL),
    re.compile(r"^[ \t]*\$\s+(.+)$", re.MULTILINE),
)

_CLI_TOKEN_PATTERNS = (
    re.compile(r"\bsudo\b"),
    re.compile(r"\b(launchctl|kickstart)\b"),
    re.compile(r"\bsed\s+-i\b"),
    re.compile(r"\b(chmod|chown)\b"),
    re.compile(r"\bpython3?\s+-c\b"),
    re.compile(r"\bfind\s+/Users\b"),
    re.compile(r"\bopenclaw\s+\w+"),   # openclaw exec-policy set, etc
)
```

A pre-filter match is necessary but not sufficient — the haiku
confirmer (§7.4) decides whether the match is *recommendation* vs
*explanation* (the *"sudo can be used to..."* prose case).

### 7.4 The haiku confirmer (required in v1, multi-criterion)

Mirroring PR #1438's Variant C, with the v1 scope expanded per the
two follow-on diagnoses [#1479](https://github.com/evolve-ops/evolve/pull/1479)
and [#1480](https://github.com/evolve-ops/evolve/pull/1480).

**Required, not optional.** The original spec deferred haiku to a
follow-up if the regex false-positive rate exceeded ~5%. That
deferral is reversed: haiku is required in Phase 4 v1. The
empirical case from
[`docs/diagnosis-exec-locked-down-fabrication-variant-2026-05-23.md`](docs/diagnosis-exec-locked-down-fabrication-variant-2026-05-23.md)
(PR #1480, Axis 1) demonstrates that a regex-only Phase 4 would miss
a known-recurring failure mode (the *"Exec is locked down in this
session context"* preface — pure prose with no shell tokens). The
shell-as-advice failure family also uses synonyms outside the
enumerated forbidden-token list (PR #1480 Mechanism 2 — "denied",
"locked down", "blocked", "restricted", "walled off", …, an
open-ended synonym pool). Regex catches the shell-shaped subset;
haiku covers the prose-shaped + synonym-shifted subset.

**Multi-criterion prompt.** A single haiku call per flagged turn,
asking three questions in one round-trip. Haiku is more than
capable of the conjunction (per #1480 Axis 2).

> *"Examine this assistant response. Does it: (a) recommend the
> operator run a shell or terminal command, (b) preface the answer
> with a claim about exec / shell / permission-tier / session-security
> capability, or (c) recommend an action whose safety depends on an
> unverified precondition (file path, staged artifact, config value)
> referenced from earlier conversation? Answer with one of: yes/a,
> yes/b, yes/c, no — plus a one-line reason."*

The three criteria map to three known-recurring failure modes:

1. **Shell-recommendation** (PR #1438) — the model recommends a
   shell command. Existing spec scope.
2. **Permission-tier fabrication** (PR #1480) — the model prefaces
   the answer with a claim about exec / shell / permission-tier /
   session-security capability ("*Exec is locked down in this session
   context*", "*I'd need elevated exec access*", "*Exec is denied in
   my security context*", …). New scope per the 2026-05-23 transcript.
3. **Precondition staleness** (PR #1479) — the model recommends an
   action whose safety depends on a precondition (a file existing, a
   staged artifact still being valid, a config value being set)
   referenced from an earlier conversation turn without a same-turn
   tool call to re-verify it. New scope per the 2026-05-23 cron-caps
   `/tmp/security-bot-jobs-patched.json` transcript.

**Cost:** ~$0.0005 per flagged turn (one round-trip), ~5-10% of
turns flagged. Net cost remains bounded. Single round-trip is
cheaper than three sequential single-criterion calls and the prompt
length is well within haiku's context.

**Decision branching:** the haiku return tells the inspector which
substitution shape to apply (see §7.4.1).

- `yes/a` → shell-recommendation substitution (existing spec behavior)
- `yes/b` → permission-preface stripper (PR #1480)
- `yes/c` → precondition-staleness substitution (PR #1479)
- `no` → release the response as-is (regex caught a prose mention,
  not a recommendation or fabrication)

### 7.4.1 Substitution shapes by failure mode

The three haiku-positive branches each have a distinct substitution
shape, calibrated to the failure mode. Shell-recommendation strips
the snippet outright; permission-preface strips one clause and
keeps the rest; precondition-staleness rejects the action and asks
the model to re-verify before re-trying.

**(a) Shell-recommendation → strip snippet, surface UI alternative.**

Existing spec behavior (§7.2 / §7.8). The CLI block is removed and
substituted with a Rung 2 / Rung 4 message. Stub:

> *"I started to recommend a shell command — that's against my
> rules. Let me look up the right registered tool or UI surface."*

If `_ui_alternative_for(cli_blocks)` returns a match, the stub is
appended with the page + button reference.

**(b) Permission-tier fabrication → strip opening clause, keep rest.**

Per PR #1480's "*The implication for the inspector*" subsection
(line 196-199 of the diagnosis): this is a clean substitution that
**removes only the preface clause** while preserving the
constructive remainder of evo's reply. The fact the model is
prefacing with — `tools.exec.security="deny"` on the primary bot —
is real config; the bug is referencing it in front of the operator,
not the underlying claim. The reply's remainder (the priority list,
the three things, the operator-facing advice) is usually fine.

Substitution shape: drop the opening sentence(s) up to (but not
including) the first constructive sentence. Stub for the dropped
clause, prepended:

> *"(I started to preface with a capability claim — skipping that;
> here's the answer:)*"

Or, when the remainder doesn't survive cleanly on its own (the
preface and the body are interwoven), fall back to a Rung 4
substitution with the operator-visible message:

> *"I started to preface with a capability claim instead of just
> answering. Let me try again — what did you ask?"*

The first form is preferred; it preserves user time. Use a haiku
follow-up call to extract the constructive remainder cleanly if the
boundary is ambiguous (cheap; ~$0.0005).

**(c) Precondition staleness → reject + log + advise re-verify.**

Per PR #1479's recommended fix shape Section (c) (line 320-349 of
the diagnosis): the action recommendation is rejected outright,
the operator is told why, and the model is offered the chance to
re-trigger with the precondition flagged. The risk class is
acting on a stale assertion (the 2026-05-23 transcript would have
silently reverted a live cap to a 2-day-old staged JSON if the
operator had run it).

Substitution shape: reject the action recommendation, leave the
non-action remainder of the reply if it survives, and surface a
precondition-flag to the operator. Stub:

> *"I started to recommend an action based on state from earlier in
> our conversation. Let me re-check the current state via a tool
> call before acting on this."*

Optionally, the inspector may **re-trigger the model with the
precondition flagged** as a follow-up — passing the model a
synthetic context-block of the form `<inspector-flag
kind="precondition-staleness" path="/tmp/security-bot-jobs-patched.json"
suggested-tools="config.bot" />` so the next turn calls the read
tool first. This re-trigger is a Phase 4 v1.1 enhancement; v1 ships
with the static stub and the model re-tries on the operator's next
turn if they prompt again.

The substitution stubs are wired in §7.8's `_SUBSTITUTION_STUBS`
table — see that subsection for the full keyed map after the
amendments.

### 7.5 The accuracy verification helpers

```python
def _verify_cli_accuracy(cli_blocks: list[str]) -> _AccuracyResult:
    """Run the §4 verification checks on each block."""
    issues: list[str] = []
    for block in cli_blocks:
        # bot_id resolution
        for m in re.finditer(r"\bsudo\s+-u\s+(\w+)\b", block):
            bot_id = m.group(1)
            try:
                from evolve_config import bot_home
                _ = bot_home(bot_id)
            except (KeyError, Exception):
                issues.append(f"unknown bot_id: {bot_id}")

        # path validity — `/Users/<x>/.openclaw` must resolve via bot_home
        for m in re.finditer(r"/Users/(\w+)/\.openclaw", block):
            user = m.group(1)
            if user not in _KNOWN_BOT_ACCOUNT_NAMES:
                issues.append(f"unknown path user: {user}")

        # schema-safety: no sed/awk on JSON
        if re.search(r"\b(sed|awk)\b.*\.json\b", block):
            issues.append("text-mutation tool on JSON file")

    return _AccuracyResult(
        has_hallucination=bool(issues),
        reason="; ".join(issues),
    )
```

`_KNOWN_BOT_ACCOUNT_NAMES` is built at proxy-import time from the
network.json's `bots[].account_name` (or `bot_id` when account_name
is the same). The set is small (≤20 bots in practice) and cheap to
build.

### 7.5.1 Regression test fixture

Phase 4 implementation must include a regression-test fixture that
pins known-recurring failure-mode variants. The fixture is the
source of truth for Phase 4's accuracy verification tests and is
extended each time a new variant surfaces in transcripts.

Three categories, each populated from prior transcripts (PR #1438's
damage audit + PR #1480's four-variant enumeration + PR #1479's
precondition-staleness transcript):

```python
# packages/admin/tests/test_inspector.py
#
# Known fabrication-pattern variants — must be caught by haiku
# (regex can't see them; they're prose, not shell snippets).
EXEC_FABRICATION_VARIANTS = [
    "I'd need elevated exec access on this session",       # 2026-05-15
    "Exec is denied in my security context",               # 2026-05-19
    "Exec is denied in this session",                      # 2026-05-19
    "Exec is locked down in this session context",         # 2026-05-23 (PR #1480)
    # New variants added here as transcripts surface them.
    # The synonym pool is open-ended (PR #1480 Mechanism 2);
    # haiku is the structural defense, not the enumeration.
]

# Known shell-as-advice patterns — regex catches each; haiku
# confirms recommendation-vs-prose-mention.
SHELL_AS_ADVICE_PATTERNS = [
    ("sudo cp /tmp/security-bot-jobs-patched.json /Users/security-bot/.openclaw/cron/jobs.json",
     "expected: catch + precondition-staleness OR shell-recommendation substitution"),
    ("sudo -u team-bot-b sed -i '' 's/.../...' /Users/team-bot-b/...",
     "expected: catch + bot_id/account_name mismatch flagged + schema-safety reject (sed on JSON)"),
    ("sudo /bin/launchctl kickstart -k system/ai.openclaw.team-bot-a-gateway",
     "expected: catch + UI-alternative substitution on admin_ui surfaces"),
    ("sudo chmod +a 'user:evolve allow read,write' /Users/<bot>/.openclaw/...",
     "expected: catch + UI-alternative (Maintenance → Repair ACLs) substitution"),
    ("python3 -c 'import json; ...' < openclaw.json",
     "expected: catch + schema-safety / non-templated-CLI substitution"),
    # Each row corresponds to an audit-table entry in PR #1438.
]

# Known precondition-staleness patterns — pure prose; only
# haiku catches them (PR #1479).
PRECONDITION_STALENESS_PATTERNS = [
    ("If /tmp was cleared since Wednesday, let me know and I'll re-stage",
     "expected: catch + advise re-verify"),
    ("assuming the file is still there",
     "expected: catch + advise re-verify"),
    ("from my earlier turn I had staged the patch at /tmp/...",
     "expected: catch + advise re-verify"),
    ("unless that's changed since last we spoke",
     "expected: catch + advise re-verify"),
]

# Known prose-mention NEGATIVES — must NOT be substituted.
# These are legitimate explanatory references to capability state,
# answering an operator's direct question.
PROSE_MENTION_NEGATIVES = [
    ("Your tools.exec.security is set to 'deny' — that's the OC field "
     "governing whether OC honors shell calls inside this bot's session.",
     "expected: pass through; operator asked what the field's set to"),
    ("In Bash you'd write `sudo cp source dest` but that's not how I'd "
     "do it here — I'll use action.bot.update_behavior_config instead.",
     "expected: pass through; explanatory contrast, not recommendation"),
    ("/tmp/ is where staging files live; macOS sweeps it periodically",
     "expected: pass through; prose explanation of a path"),
]
```

The fixture lives next to the inspector tests
(`packages/admin/tests/test_inspector.py`). Each pattern is paired
with the expected inspector outcome so a regression that breaks
either the catch path or the pass-through path fails the test.

**Maintenance rule.** Each time a new variant appears in a
transcript and gets diagnosed, the fixture is extended in the same
PR that ships the fix or the diagnosis. This is the explicit
answer to §9.4's "who owns the forbidden-token list" — the legacy
Command Reference seeds the regex; the fixture seeds the
test suite. Open question §9.9 (added below) tracks the maintenance
discipline.

### 7.6 The UI-alternative lookup map

A new module `packages/admin/evolve_admin/evo/ui_alternatives.py`:

```python
_UI_ALTERNATIVES: list[tuple[re.Pattern, str]] = [
    # context-pruning TTL editing
    (re.compile(r"contextPruning\.ttl|cm-prune-ttl"),
     "Cost Optimization page → bot section → Context pruning TTL → "
     "select the new value, click Save"),

    # gateway restart
    (re.compile(r"launchctl\s+kickstart.*?openclaw\.(\w+)-gateway"),
     "Dashboard → bot tile → **Restart Gateway**"),

    # plugin enable/disable
    (re.compile(r"plugins\[.*?\]\s*=|plugins\.pop"),
     "Plugins page → bot row → **Enable** / **Disable** button"),

    # ACL repair
    (re.compile(r"chmod\s+\+a.*?evolve\s+allow"),
     "Maintenance page → Repair ACLs (or `action.bot.repair_acls` tool)"),

    # ... one entry per common shell pattern in the audit
]


def lookup_ui_alternative(cli_blocks: list[str]) -> str | None:
    """Return the first matching UI-alternative description, or None."""
    blob = "\n".join(cli_blocks)
    for pattern, description in _UI_ALTERNATIVES:
        if pattern.search(blob):
            return description
    return None
```

**Maintenance.** The map is **hand-curated**. Open question (§9)
is whether to derive it from page_context's `available_actions`
field at request-time — that's a Phase 6 optimization.

### 7.7 Telemetry

Every inspector event writes to `{shared_dir}/logs/inspector.jsonl`:

```json
{
  "ts": "2026-05-22T14:32:15Z",
  "event": "cli_on_mobile",
  "session_id": "admin-ui-recommendations-...",
  "surface": "admin_ui",
  "surface_type": "mobile",
  "preference": "either",
  "original_cli_excerpt": "sudo -u team-bot-a sed -i ...",
  "substituted_text_excerpt": "This chat is on mobile — ...",
  "reason": null
}
```

This is the diagnostics surface for the dev team: which surfaces
trigger substitutions most often, which CLI patterns the model keeps
reaching for, where new tool gaps should be prioritized.

### 7.8 Substitution stubs

A small set of fixed operator-facing messages, each calibrated to
its failure mode. Three new entries land via the §7.4 amendments
(PRs #1479 + #1480):

```python
_SUBSTITUTION_STUBS = {
    # Shell-as-advice family (existing; PR #1438)
    "hallucinated_cli": (
        "I started to recommend a shell command but couldn't "
        "verify it. Let me look up the right registered tool or "
        "UI surface instead."
    ),
    "shell_recommendation_no_ui_alt": (
        "I started to recommend a shell command — that's against "
        "my rules. Let me look up the right registered tool or UI "
        "surface."
    ),
    "cli_on_mobile_with_ui_alt": (
        "This chat is on mobile — I can't recommend CLI commands "
        "you'd need to run from a terminal. The same change is on "
        "the admin UI at: {ui_alternative}"
    ),
    "cli_on_mobile_no_ui_alt": (
        "This chat is on mobile and I don't have a UI route for "
        "this yet. I've logged it as a tool gap so it gets "
        "prioritized."
    ),
    "cli_on_laptop_ui_preferred_no_ui_alt": (
        "You prefer UI over CLI, and I don't have a UI route for "
        "this yet. Logging the gap so it gets prioritized."
    ),

    # Permission-tier fabrication family (new; PR #1480)
    # Prepended to the constructive remainder of the reply.
    "permission_preface_prepend": (
        "(I started to preface with a capability claim — skipping "
        "that; here's the answer:)\n\n"
    ),
    # Fallback when the constructive remainder doesn't survive
    # clean preface-stripping.
    "permission_preface_fallback": (
        "I started to preface with a capability claim instead of "
        "just answering. Let me try again — what did you ask?"
    ),

    # Precondition-staleness family (new; PR #1479)
    "precondition_staleness": (
        "I started to recommend an action based on state from "
        "earlier in our conversation. Let me re-check the current "
        "state via a tool call before acting on this."
    ),
}
```

The stubs are deliberately short — the operator already had context
from their question; the stub explains the substitution, not the
underlying task. The permission-preface-prepend stub is a *prefix*
to the model's own constructive remainder rather than a full
substitution; per PR #1480 the remainder is usually useful and
shouldn't be discarded.

---

## 8. Phased implementation plan

### Phase 1 — Surface plumbing + AGENTS.md restructure + empty-reply fix

**One PR. ~1 day.**

Deliverables:
- `surface_type` plumbed through `page_context` →
  [`format_page_context`](packages/admin/evolve_admin/evo/proxy.py:562)
  → `<page-context>` XML attribute.
- `surface` line in `<session-context>` block (§2.3.4).
- AGENTS.md surgery: §5a help-style decision tree section; §5b /
  §5c surface conditioning on Command Reference + Privilege
  Boundary; §5d shell-snippet rule rewrite; §5e closing-summary
  carve-out; §5f Cost Measures common-operations row.
- Empty-reply fallback (PR #1437 Priority 1): proxy reads the OC
  session JSONL for tool calls in the failed run and synthesizes a
  yellow-bubble result.
- `proxy_warn` source taxonomy (PR #1437 Priority 2): separates
  "we ran the model and it (apparently) did the work" from
  "subprocess error."
- AGENTS.md tests updated per §5g.
- File OC upstream issue for *"agent CLI exits rc=0 with empty
  payloads when loop terminates mid-flight"* (PR #1437 Priority 4).

Dependencies: none — this is the foundation phase.

Test surface:
- `tests/test_evo_agents_md_guards.py` — new guards per §5g
- `tests/test_format_page_context.py` — `surface_type` attribute
  appears when supplied
- `tests/test_format_session_context.py` — `Surface:` line appears
  when `surface` is in the session_context dict
- `tests/test_send_to_evo_empty_reply_fallback.py` — synthesized
  result on empty payload when session JSONL has tool calls in the
  run

LOC: ~400 (mostly AGENTS.md content + ~80 LOC empty-reply
fallback).

### Phase 2 — `action.bot.update_behavior_config` tool

**Separate PR. ~½ day.**

Closes the team-bot-a/team-bot-b TTL gap immediately. Uses the same L2 applier
pattern as PR #1431.

Deliverables:
- `packages/analyzer/arbiter/appliers/behavior_config.py` with
  the new whitelist + writer round-trip.
- Tool registered into evo's tool surface.
- `tests/test_behavior_config_applier.py` mirrors
  `test_permissions_applier.py`.
- Update §5f's common-operations row to point at
  `action.bot.update_behavior_config` now that the tool exists.

Dependencies: Phase 1 (the surface plumbing makes the AGENTS.md
guidance coherent).

LOC: ~200.

### Phase 3 — Operator preference setting

**Separate PR. ~½ day.**

Deliverables:
- `network.json::operators.<id>.help_style_preference` schema +
  default-value handling in `config.py`'s loader.
- Settings page UI for the field (§3.5.3).
- `home_chat_routes.py` reads the value and injects it into
  `session_context` (§3.5.1).
- `format_session_context` renders the `Help style preference:`
  line (§3.5.2 — already in Phase 1 plumbing; this phase wires
  the data flow).
- AGENTS.md addition (the in-thread-preference rule from §3.3).
- `tests/test_help_style_preference.py` — covers the resolution
  order from §3.4.

Dependencies: Phase 1.

LOC: ~250.

### Phase 4 — Surface-aware inspector

**Separate PR. ~1.5 days.** (Bumped from ~1 day to account for the
required-v1 haiku confirmer + multi-criterion prompt + three
substitution shapes per the [#1479](https://github.com/evolve-ops/evolve/pull/1479)
and [#1480](https://github.com/evolve-ops/evolve/pull/1480) amendments
to §7.4 and §7.4.1.)

Deliverables:
- `packages/admin/evolve_admin/evo/inspector.py` with the
  regex pre-filter + haiku confirmer + accuracy verification stages
  (§7.2-7.5).
- **Haiku confirmer is required v1, not optional** (per §7.4
  amendment). Multi-criterion prompt covering shell-recommendation,
  permission-tier fabrication, and precondition staleness in one
  round-trip.
- Three failure-mode-specific substitution branches (§7.4.1):
  shell-recommendation strips snippet + surfaces UI alternative;
  permission-tier fabrication strips opening clause + keeps
  remainder; precondition staleness rejects + asks for re-verify.
- Wired into `send_to_evo` per §7.1.
- `packages/admin/evolve_admin/evo/ui_alternatives.py` — the
  hand-curated map (§7.6).
- Telemetry log per §7.7 (events include `precondition_staleness`
  and `permission_tier_fabrication` in addition to the existing
  shell-recommendation events).
- `tests/test_inspector.py` — driven by the regression fixture in
  §7.5.1:
  - one test per audit row in PR #1438's damage table
    (`SHELL_AS_ADVICE_PATTERNS`)
  - one test per fabrication variant in
    `EXEC_FABRICATION_VARIANTS`, including the 2026-05-23 "locked
    down in this session context" case (PR #1480)
  - one test per `PRECONDITION_STALENESS_PATTERNS` row,
    including the 2026-05-23 cron-caps `/tmp` case (PR #1479)
  - negative tests for `PROSE_MENTION_NEGATIVES` (explanatory
    references that must pass through)
  - legitimate Telegram-CLI passes-through.

Dependencies: Phase 1 (for the session-context `surface` /
`surface_type` / `help_style_preference` plumbing the inspector
reads).

LOC: ~700 (inspector + map + haiku confirmer + tests; up from the
original ~500 estimate for regex-only + simpler tests).

### Phase 5 — `action.bot.exec_canonical` for templated commands

**Separate PR. ~1 day.**

Deliverables:
- `packages/analyzer/arbiter/exec_canonical.py` with the
  `_COMMAND_REGISTRY` (§6b).
- ~20 command_kinds covering the legacy Command Reference verbs.
- Audit log at `{shared_dir}/logs/exec_canonical.jsonl`.
- Tool registered in evo's tool surface.
- AGENTS.md update: Command Reference verbs now have a `(via
  action.bot.exec_canonical command_kind="X")` pointer alongside
  the literal command.
- `tests/test_exec_canonical.py` — per-command rendering + bot_id
  resolution + dry_run + unknown-kind reject.

Dependencies: Phase 1 (the Command Reference is the source of
truth for the registry).

LOC: ~400.

### Phase 6 — UI-alternative lookup map maintenance + Telegram preference command

**Separate PR (or folded into Phase 4). ~½ day.**

Deliverables:
- Expand the UI-alternatives map based on Phase-4 telemetry data
  (which patterns are firing most often).
- `evo set preference {ui|cli|either}` Telegram keyword handler
  (§3.2 — low priority but completes the round-trip for
  Telegram-only operators).
- Optional: experiment with deriving the map from page_context's
  `available_actions` field (§9 open question).

Dependencies: Phase 4 telemetry data (so the map expansion is
data-driven rather than guesswork).

LOC: ~150.

### Phased total

~2100 LOC + ~4.5 agent-days, spread across 6 PRs. Each PR is
self-contained and independently testable; later phases enrich
earlier phases but don't break them. (Up from the original ~1900
LOC / ~4 days estimate to absorb the Phase 4 haiku-required-v1
expansion per PRs [#1479](https://github.com/evolve-ops/evolve/pull/1479)
and [#1480](https://github.com/evolve-ops/evolve/pull/1480).)

---

## 9. Open questions

These don't block the spec from landing; they're decisions for
Phase 1 implementation or later.

### 9.1 `surface_type` detection

**Question:** client-side viewport check vs server-side
User-Agent vs both?

**Recommendation:** client-side viewport per §2.3.2; server-side
UA as a fallback for clients that don't send `surface_type`.
Defensive default: `"laptop"` when uncertain.

**Decision needed in:** Phase 1.

### 9.2 `help_style_preference` storage location

**Question:** in `network.json::operators` (shared file, single
write path) vs per-operator file under `{shared_dir}/operators/<id>.json`
(scales for multi-operator pods)?

**Recommendation:** `network.json::operators` for v1 (one operator);
re-evaluate when multi-operator support is in scope.

**Decision needed in:** Phase 3.

### 9.3 Telegram operator preference setting mechanism

**Question:** how do Telegram-only operators set their preference
without ever touching the admin UI? Settings page is the canonical
surface but isn't reachable from Telegram.

**Recommendation:** `evo set preference {ui|cli|either}` keyword
command (§3.2). Low priority — only matters if an operator never
uses the admin UI at all, which is rare in practice.

**Decision needed in:** Phase 6 (or earlier if needed).

### 9.4 Inspector regex maintenance ownership — RESOLVED

**Question (original):** who owns the forbidden-token list? Is it
generated from the legacy Command Reference + audit findings?

**Resolution (per PRs [#1479](https://github.com/evolve-ops/evolve/pull/1479)
+ [#1480](https://github.com/evolve-ops/evolve/pull/1480), 2026-05-23):**
the legacy Command Reference in AGENTS.md IS the regex's source of
truth. Regex tokens are derived from its enumerated commands. The
regression-test fixture in §7.5.1 is the test-time enforcer: every
audit-row pattern in `SHELL_AS_ADVICE_PATTERNS` must be matched.
The haiku confirmer (§7.4) covers the synonym pool the regex can't
enumerate — see §9.7 below for that resolution.

### 9.5 Structural intent CLI emission shape

**Question:** does the model emit `exec_canonical(...)` as a
real tool call, or as a special block in its text the proxy
parses?

**Recommendation:** real tool call. Tool calls have stable
schemas (validated by OC), telemetry hooks, and authority gates.
A special text block re-invents tool dispatch worse.

**Decision needed in:** Phase 5.

### 9.6 UI-alternative lookup: hand-curated vs derived?

**Question:** hand-curated map vs derived from
`page_context.summary.available_actions` at request-time vs
LLM-deduced?

**Recommendation:** hand-curated for v1 (small set, ~10 entries
covers the audit). Revisit deriving from `available_actions` if
the map grows past ~30 entries and maintenance becomes painful.

**Decision needed in:** Phase 6 or later.

### 9.7 Inspector haiku-confirmer rollout — RESOLVED

**Question (original):** ship Phase 4 with regex-only, or include
the haiku confirmer from day 1?

**Resolution (per PR [#1480](https://github.com/evolve-ops/evolve/pull/1480),
2026-05-23):** **haiku is required v1, not optional.** The empirical
case from 2026-05-23 ("Exec is locked down in this session
context") demonstrates regex-only Phase 4 misses a known-recurring
failure mode (permission-tier fabrication is pure prose with no
shell tokens). The shell-as-advice family also uses synonyms outside
the enumerated forbidden-token list. Spec §7.4 was amended to make
haiku mandatory; the multi-criterion prompt covers all three known
failure modes (shell-recommendation, permission-tier fabrication,
precondition staleness) in one round-trip.

### 9.8 `action.bot.update_behavior_config` whitelist scope

**Question:** which behavior-config fields are in the initial
whitelist? Cost Measures exposes several; should they all be in
the tool's whitelist on day 1?

**Recommendation:** start with the fields surfaced on the Cost
Measures editor — that's the audited-safe set. Extend as new
fields appear on the page.

**Decision needed in:** Phase 2.

### 9.9 Precondition-staleness check — distinguishing prose mentions from recommendations

**Question (new, per PR [#1479](https://github.com/evolve-ops/evolve/pull/1479)):**
how does the precondition-staleness check distinguish *prose
mentions* of paths (legitimate, e.g. operator asks "what's in
`/tmp/`?" and evo's answer references the path explanatorily) from
*recommendations* referencing paths (suspect, e.g. evo says
"`sudo cp /tmp/security-bot-jobs-patched.json …`" without verifying the
file is still there)?

**Recommendation:** the haiku confirmer's multi-criterion prompt
(§7.4) is the primary mechanism. Criterion (c) asks specifically
about *"recommend an action whose safety depends on an unverified
precondition"* — that wording bounds the check to
recommendation-vs-mention. The negative cases in
`PROSE_MENTION_NEGATIVES` (§7.5.1) pin the distinction in the test
fixture. If the false-positive rate is high in production telemetry,
tighten the haiku prompt or add a secondary check (e.g.,
imperative-tense + shell-shaped boundary).

**Decision needed in:** Phase 4 implementation; revisit per
telemetry data after first 30 days.

### 9.10 Regression-fixture maintenance discipline

**Question (new, per PRs [#1479](https://github.com/evolve-ops/evolve/pull/1479)
+ [#1480](https://github.com/evolve-ops/evolve/pull/1480)):** how is
the regression fixture (§7.5.1) maintained over time as new variants
emerge? Diagnoses surface new fabrication / shell-as-advice /
staleness variants on a ~2-3-day cadence (per PR #1480's tally —
four exec-fabrication variants in nine days). The fixture is only
as good as its currency.

**Recommendation:** convention — each new diagnosis that surfaces a
novel variant ships the fixture extension in the same PR (or in the
immediate follow-up PR that fixes the diagnosed bug). The
`EXEC_FABRICATION_VARIANTS` / `SHELL_AS_ADVICE_PATTERNS` /
`PRECONDITION_STALENESS_PATTERNS` lists are the canonical home; PR
review should check fixture additions exist when a diagnosis names
a new variant. Consider a periodic (monthly?) audit pass to confirm
the fixture covers the current production-transcript variant pool.

**Decision needed in:** Phase 4 implementation (establish the
convention); enforcement is process, not code.

---

## 10. Why this design vs alternatives

### 10.1 Why an escalation hierarchy (rather than surface-only rules)?

Operator preference is orthogonal to surface — surface tells you
*what's possible*, preference tells you *what's wanted*. Conflating
them produces poor UX for operators with non-default preferences:

- A Telegram user with `preference == "ui"` is real (the locked-in
  principle quotes this case directly) and isn't served by "Telegram
  → CLI" hard rules.
- An admin-UI laptop user with `preference == "cli"` is also real
  and isn't served by "admin-UI → never CLI" hard rules.

The hierarchy + axes design captures both axes cleanly. Surface is
the *ceiling* (mobile blocks CLI absolutely); preference is the
*tie-breaker* below the ceiling.

**Rejected:** flat per-surface rules. They produce the wrong UX for
non-default-preference users in every surface.

### 10.2 Why surface-conditional rules (rather than a universal CLI ban)?

Evo's **primary use case** is conversational sysadmin via Telegram,
where CLI is the system. A universal CLI ban (the first reaction to
PRs #1437/#1438) breaks the primary use case to fix a failure mode
that only happens on the secondary surface. That's the inverse of
what good design does — fix the bug in the surface where it occurs,
without breaking the surface where the same pattern is correct.

**Rejected:** the post-#1428 reaction of *"ban shell snippets,
period."* The transcripts that triggered the spec are all admin-UI
turns; Telegram's CLI workflows have been working fine.

### 10.3 Why accuracy verification (rather than trust the model)?

The PR #1438 damage audit found 4/8 actionable snippets would have
failed in non-obvious ways: bot_id ≠ account_name confusion (3/8),
schema-coupled `sed` patterns (1/8), wrong API surface (1/8). The
operator didn't run any of them because they noticed in time. The
next operator may not.

Trust-but-verify has near-zero cost (regex + a small whitelist
table) and bounded latency. Trust-only has documented damage
(the chown -R loop would have required a redeploy across six
bots). The asymmetry is decisive.

**Rejected:** trusting the model. The audit data ruled this out;
the failure mode is real and recurring.

### 10.4 Why structured intent for canonical commands (rather than literal CLI verification)?

`exec_canonical(command_kind="restart_gateway", bot_id="team-bot-a")` is
unambiguous and unhallucianable. The proxy renders the literal
command using `bot_home("team-bot-a")` — there's no string the model
typed that could be wrong. Verification at the literal-CLI level
catches **most** errors, but only structured-intent eliminates the
failure mode by construction.

The two layers compose: the inspector handles ad-hoc CLI (Telegram
power-user case); `exec_canonical` handles the canonical workflow
(the 20 Command Reference verbs that cover ~80% of admin actions).

**Rejected:** only-inspector (no exec_canonical). Inspector alone
keeps the failure mode reachable for the canonical commands;
verification rewrites are more error-prone than construction.

### 10.5 Why per-operator preference (rather than per-surface defaults only)?

Surface defaults aren't enough because **the same operator on the
same surface may want different styles**, and surface alone doesn't
capture that. The Plex-test operator on admin-UI laptop wants UI
(the default); a power-user on admin-UI laptop wants CLI; without
preference these two operators get the same suggestion.

Per-operator preference also enables the multi-operator pod future:
when a pod has both a sysadmin who wants CLI and an end-user who
wants UI, surface defaults can't distinguish them.

**Rejected:** per-surface only. Doesn't capture the variance the
locked-in principle calls out explicitly.

### 10.6 Why `<session-context>` for surface (rather than only `<page-context>`)?

`<page-context>` is absent on Telegram by design — its presence
encodes "admin UI." If surface lived only on `<page-context>`,
Telegram would have to special-case the absence everywhere. Lifting
surface into `<session-context>` gives every code path the same
authoritative read for the operator's surface, regardless of which
chat channel is in play.

**Rejected:** keeping surface on `<page-context>` only. Forces
every consumer to handle "no page-context → must-be-telegram" logic
separately. Lifting to session-context makes the absent-page-context
case explicit (`Surface: telegram`).

---

## 11. References

### 11.1 Diagnoses synthesized by this spec

- PR #1438 — Chip F: the team-bot-a/team-bot-b TTL audit, the inspector
  variants comparison, the AGENTS.md contradiction analysis. The
  4-rung escalation is this spec's synthesis of the four
  recommended fixes (teaching + tool gap + UI signpost +
  inspector) into a unified framework.
- PR #1437 — Chip E: the empty-reply fallback design and the
  admin-UI closing-summary carve-out. Phase 1 of this spec
  bundles those fixes.
- PR #1479 — Chip #84: the 2026-05-23 cron-caps
  `/tmp/security-bot-jobs-patched.json` transcript. Motivated the
  precondition-staleness criterion in §7.4's multi-criterion haiku
  prompt and the precondition-staleness substitution shape in
  §7.4.1(c).
- [`docs/diagnosis-exec-locked-down-fabrication-variant-2026-05-23.md`](docs/diagnosis-exec-locked-down-fabrication-variant-2026-05-23.md)
  (PR #1480) — Chip #85: the 2026-05-23 *"Exec is locked down in
  this session context"* fabrication variant. Motivated the
  haiku-required-v1 promotion (§7.4, §9.7), the permission-tier
  fabrication criterion in the multi-criterion prompt, and the
  permission-preface-stripper substitution shape in §7.4.1(b).

### 11.2 Sibling architectural patterns

- [`feedback_generators_consider_intent`](memory:feedback_generators_consider_intent)
  — config_intent system. Generators (this spec's siblings on the
  proposal side) need to reason about intent before proposing
  reverts. Same trust-but-verify principle; same operator-context-
  is-load-bearing rule.
- [`project_l1_l2_applier_architecture`](memory:project_l1_l2_applier_architecture)
  — L2 applier pattern used by `action.bot.update_behavior_config`
  (Phase 2) and `action.bot.exec_canonical` (Phase 5). The behavior-
  config applier mirrors `UpdatePermissionConfigApplier` with a
  different whitelist.
- [`project_evo_oc_native_architecture`](memory:project_evo_oc_native_architecture)
  — evo's three surfaces (admin UI, Telegram, indirect cross-bot
  keyword). All three route through the same evo gateway and thus
  the same proxy — meaning the inspector + session-context
  surface line cover all three from a single code path.

### 11.3 Plex-test + Marcus

- [`feedback_design_constraint_mildly_tech_capable`](memory:feedback_design_constraint_mildly_tech_capable)
  — every primary surface must work for the Plex-installing,
  Home-Assistant-running operator. The `"either"` default
  (favors UI on admin-UI, CLI on Telegram) honors this constraint;
  the `"ui"` opt-in is the lockdown for operators who explicitly
  want no CLI exposure.
- [`feedback_bot_id_not_account_name`](memory:feedback_bot_id_not_account_name)
  — the canonical accuracy bug (team-bot-b runs on personal-bot-user). The
  inspector's bot_id-resolution check is the structural fix; the
  Phase 5 `exec_canonical` tool eliminates the failure mode by
  construction.

### 11.4 AGENTS.md current state

The §5 surgery is grounded in these line ranges of
[`packages/analyzer/evolve_bot/AGENTS.md`](packages/analyzer/evolve_bot/AGENTS.md):

- Lines 106-128 — current Surface awareness section (the v1.5
  baseline; §5a adds the help-style decision tree right below it).
- Line 234 — current never-propose-shell rule (§5d rewrites).
- Lines 912-1136 — Command Reference (§5b gates with
  Telegram-conditioning preface).
- Lines 1169-1186 — Privilege Boundary (§5c gates the same way).

### 11.5 Cross-references for implementation

- [`packages/admin/evolve_admin/evo/proxy.py:226`](packages/admin/evolve_admin/evo/proxy.py:226)
  — `format_session_context` (§2.3.4 patch site).
- [`packages/admin/evolve_admin/evo/proxy.py:562`](packages/admin/evolve_admin/evo/proxy.py:562)
  — `format_page_context` (§2.3.3 patch site).
- [`packages/admin/evolve_admin/evo/proxy.py:803`](packages/admin/evolve_admin/evo/proxy.py:803)
  — `send_to_evo` (§7.1 inspector wire-in site).
- [`packages/admin/evolve_admin/evo/proxy.py:915`](packages/admin/evolve_admin/evo/proxy.py:915)
  — empty-reply fallback site (Phase 1 — Priority 1 from PR #1437).
- [`packages/admin/evolve_admin/web/home_chat_routes.py:209`](packages/admin/evolve_admin/web/home_chat_routes.py:209)
  — `session_context` assembly (§3.5.1 patch site).
- [`packages/admin/evolve_admin/web/index.html:32655`](packages/admin/evolve_admin/web/index.html:32655)
  — `_evoDrawerContextPack` (§2.3.1 patch site).
- [`packages/analyzer/arbiter/appliers/permissions.py:38-50`](packages/analyzer/arbiter/appliers/permissions.py:38)
  — `_ALLOWED_FIELDS` whitelist; Phase 2 mirrors with the
  behavior-config whitelist.
- [`packages/analyzer/permissions/writer.py:83`](packages/analyzer/permissions/writer.py:83)
  — `write_openclaw_fields` (shared L2 writer; Phase 2 reuses).
