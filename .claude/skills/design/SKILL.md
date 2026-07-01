---
name: design
description: Natural-language intake — describe a piece of work or design issue in plain words and this ROUTES it to the right aspect (and opens that coordinator), proposes a new aspect only if nothing fits, or flags an overlap/merge. So you focus on the design, not on remembering which of 20+ aspects owns what.
---

The operator describes work in plain language instead of naming an aspect. Your job is **intake
routing**: find the right home, state it, then become that coordinator. Aspects are the background
engine; the operator's interface is design intent. (`/meta <aspect>` stays the explicit shortcut
for when they already know the home.)

The intake can also be a **GitHub issue**: a bare or `#`-prefixed number (`/design 2656`,
`/design #2656`) or an issue URL (`/design https://github.com/<org>/<repo>/issues/2656`). When the
arg is an issue ref, fetch the normalized record and triage it **identically to free text** —
title + body + comments are the description. Anything that isn't an issue ref is free text (today's
behavior).

- **Fetch:** run `python3 tools/meta-issue <ref>` (repo-pure; only talks to GitHub via `gh`). It
  returns `{number, url, state, title, body, author, age_days, labels[], aspect_hints[],
  agent_able, proof, comments[]}`. Classify from `title` / `body` / `comments[]` exactly as you
  would free text.

## 1. Load the routing knowledge

- The **"Surface ownership (the routing map)"** table in `docs/META-session-guide.md` +
  the **Aspect registry** in `docs/META-aspect-registry.md` — surface → owning aspect, and
  each aspect's mission/invariants.
- Each aspect's **mission + backlog** — glob `meta-state/*.json` (skip `_README`). The missions
  are your richest classifier signal; the map resolves page/surface mentions.

## 2. Classify the described work by CONTENT and pick the outcome

- **Label = routing prior, never a verdict (issue intake only).** The record's `aspect_hints[]`
  are the label prefixes before `:` (e.g. `edr` from `edr:agent-able`). Treat them as a *prior*
  that biases classification toward that aspect — labels go stale, so the CONTENT classification
  stays authoritative. If content and label disagree, **trust content** and note the mismatch
  (*"labelled `edr` but reads as `reports` — routing on content"*). `agent_able` + `proof` are a
  human's "this is dispatchable, here's the acceptance test" hint, useful when the routed aspect
  considers chipping it directly.
- **Clear fit (one aspect):** say *"Routing to `<aspect>` — `<one-line why>`."* Then **retitle the
  session: lead your very next response with `META <aspect>` on its own first line** — the canonical
  title `/meta` sets (`docs/META-bootstrap.md` §"Naming convention" rule 1), so a routed session
  becomes indistinguishable from one launched by `/meta <aspect>`. Bootstrap that aspect (the `/meta`
  procedure) and proceed as its coordinator. Always offer a one-word override: *"not `<aspect>`? name
  the right one."*
- **Spans layers** (e.g. a page's *numbers* vs its *look*): name the PRIMARY content owner for the
  design discussion and the collaborator layer (usually `ui` for presentation). Design in the
  primary; note the other slice will route to its owner via deposit.
- **Ambiguous between two aspects:** ask — but lead with a **recommendation + the one axis they
  differ on** (decision-triage; never a bare "which?"). A genuine 50/50 is itself a signal those
  two aspects overlap — say so.
- **No fit → maybe a NEW aspect:** apply **carve-first** before creating anything. Is this a
  durable concern with its *own* spec + backlog + invariants, or just a sub-track / routing target
  of an existing aspect? **Default to fitting it into an existing aspect.** Only if genuinely
  distinct, propose scaffolding a new one (the "Adding a new META" protocol) — confirm-first, with
  your recommendation and the proposed id.
- **Reveals redundancy:** if the work makes two existing aspects look like one, **flag** *"consider
  merging `<A>` + `<B>`"* as a recommendation (merging is a deliberate operator op — ids are sticky
  once chips carry the `[META:<id>]` prefix — so never merge unasked).

## 3. Bias and behavior

- **Check who's already on it BEFORE designing (ADVISORY + confirm-first).** Once you've classified
  the work's aspect, run `python3 tools/meta-inflight --aspect <id> --keywords "<the work's key
  terms>"` (add `--scope "<globs>"` if the files are already obvious) — its ledger read is
  in-subprocess + one `gh` call, only the compact report returns. Intake is the *earliest* point to
  catch redundancy: if it surfaces an in-flight chip / PR / session already on this, **present it and
  let the operator merge (fold into / join the existing effort), proceed (a genuinely distinct
  slice — note why), or cancel** rather than opening a second session at cross purposes. A clean "no
  overlap" → design on. (The per-bite pre-spawn check in `/launch` step 3 is the same check at
  dispatch time; this is its intake-time twin. See `docs/spec-substrate-2026-06-15.md` §11.)
- **Prefer routing into an existing aspect over creating a new one.** Aspect proliferation is what
  makes the system unwieldy from the operator's seat; new aspects are an event, not a reflex. This
  router is the gatekeeper.
- Once routed, behave exactly as `/meta <aspect>` from there — **including the `META <aspect>`
  retitle (above) AND the `[META:<aspect>]` prefix on every chip / PR / branch this session spawns**.
  The prefix is primarily a model-applied convention; it decays whenever the aspect id isn't
  salient, and deriving the aspect mid-conversation is exactly that case — so assert it explicitly
  here rather than relying on it carrying over. **Back it with the mechanism:** once the id is
  resolved, run (best-effort, from this cwd) `bash tools/hooks/meta-active-aspect.sh write <aspect>`
  so the `prepend-meta-prefix.sh` PreToolUse hook deterministically auto-prefixes any spawned chip /
  subagent title that lacks `[META:<aspect>] ` (the safety net for the convention; see the `/meta`
  skill — harmless no-op if the helper isn't on the checkout). Then: design-sync → dispatch chips →
  `/close`.

## 4. Issue-born work: provenance + loop closure (issue intake only)

When the intake was a GitHub issue, thread it through so the work traces back and the issue closes
itself:

- **Provenance.** Any chip you spawn or `backlog` entry you deposit for this work carries the
  `issue: <N>` field (`docs/meta-ledger-schema.md`) — the source issue number. (A backlog string
  may instead embed `#N`.)
- **Loop closure.** Instruct the routed chip that its **PR body must carry `Closes #N`** — GitHub
  auto-closes the issue on merge, and the reconciler already detects closed issues. No new closing
  machinery; do not build any.
- **Write-back (recommended, confirm-first).** After routing, reflect the triage on the tracker:
  comment the outcome and label it `meta:routed:<aspect>` —
  `gh issue comment <N> --body "Routed to \`<aspect>\` …"` and
  `gh issue edit <N> --add-label meta:routed:<aspect>`. This is an ACTION on an external surface,
  so it stays **confirm-first** (consistent with the recommend-only invariant) — propose it, don't
  do it unasked.

Keep intake light: classify, state the route (with a one-word override), then get into the design.
The point is to spend the operator's attention on the *design*, not the taxonomy.
