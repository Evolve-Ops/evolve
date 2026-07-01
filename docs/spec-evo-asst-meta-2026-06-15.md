# Spec: evo-asst — the Evo admin assistant (coordinator charter, 2026-06-15)

**Status:** seed (carved via `/design` 2026-06-15)
**Aspect id:** `evo-asst`
**Design source of truth:** [spec-surface-aware-help-style-2026-05-22.md](spec-surface-aware-help-style-2026-05-22.md)
(escalation hierarchy + surface/preference/capability axes). This charter indexes it
and the `evo-*` corpus; it does not restate the framework.

---

## 1. Mission

The **Evo tray chat** — the "ask evo…" assistant panel in the admin SPA — and the
operator-facing assistant behind it. evo-asst owns the assistant's **self/surface
context, identity, the four-rung "just do it" escalation, screen grounding, and tool
access**: i.e. *how Evo reasons about who it is, where the operator is reading, and
what it can do for them*. It is the conversational operator interface, cross-cutting
across every page.

## 2. Why an aspect (carve rationale)

The Chat / Evo tray surface had **no META owner** and was absent from the routing
map, yet it has a real spec corpus (surface-aware-help-style + evo-oc-native +
evo-llm-compliance + evo-wizard + take-this-on-evo-dispatch). Folding it into
`reports` ("operator's-eye / cross-surface UX") or `user-value` ("astoundingly
useful") would dilute either mission — the assistant is a distinct cross-cutting
conversational surface, not a page's content. Operator confirmed a new aspect over
either fold (2026-06-15).

## 3. Inherited design corpus

- [spec-surface-aware-help-style-2026-05-22.md](spec-surface-aware-help-style-2026-05-22.md) — **the framework** (the four-rung escalation + the three axes + the accuracy rule)
- [spec-evo-oc-native-2026-05-19.md](spec-evo-oc-native-2026-05-19.md)
- [spec-evo-llm-compliance-2026-05-18.md](spec-evo-llm-compliance-2026-05-18.md)
- [spec-evo-wizard-2026-05-05.md](spec-evo-wizard-2026-05-05.md)
- [spec-take-this-on-evo-dispatch-2026-06-04.md](spec-take-this-on-evo-dispatch-2026-06-04.md)
- account/deploy context (NOT owned here, but adjacent): [spec-evo-account-separation-2026-05-25.md](spec-evo-account-separation-2026-05-25.md)

## 4. Boundary / hand-offs

- **Tray presentation** (tokens, primitives, drawer layout) → `ui` (co-owns presentation everywhere).
- **Each page's content truth** (is the data/copy right?) → that page's owning aspect. evo-asst owns the assistant's *reasoning/context/identity*, not the data it reads.
- **The `evo` macOS account / ACL / deploy-security separation** → `diligence`/`deploy`. (That's the service user + 0600 ACLs, not the assistant.)
- **Signal/alert producer quality** → `reports`. **Proposal generation quality** → `rsi`.

## 5. Invariants (seed — ratchet as the aspect matures)

1. **Every turn injects the assistant's own identity + surface.** A bot cannot
   observe its own context unless told each turn (memory: `bot-cannot-observe-own-routing`,
   `evolve-bot-llm-visibility`). Self-reports without injected context are confabulation.
2. **Never deflect the operator to a surface they are already on.** "Go ask via the
   admin UI chat" *from inside the admin-UI chat* is the canonical bug.
3. **Prefer Rung 1 ("just do it")** over button/CLI guidance when a registered tool +
   the authority tier allow it (surface-aware-help-style §1.1).
4. **Never hallucinate CLI / steps** — the universal accuracy rule. A fabricated
   command costs trust on top of time.
5. **The tray IS the admin-UI chat** — the most-privileged caller (admin/evolve bot,
   unrestricted cross-bot tools). Treat it as such in identity + tool gating.

## 6. First decision — the self-context bug (carve trigger)

**Symptom:** On Backup → Cloud, the operator asked the tray "why is atlas failing
backup?" Evo replied *"I'm running as a member bot and hitting authorization walls
on cross-bot tools… run this check from the evolve (admin) bot — via Telegram or the
admin UI chat,"* and handed copy-paste prompts. Triple violation: false self-identity
(invariant 1/5), deflection to the current surface (invariant 2), wrong escalation
rung (invariant 3).

**Root cause (verified in code):** the tray correctly detects and forwards
`surface=admin_ui` + `authority` + page context, and the proxy sets
`EVOLVE_CALLER_SURFACE=admin_ui` so the *tool layer* gates admin tools correctly.
But the `<session-context>` block carries **no bot identity** — the model is never
told it is the evolve/admin bot. So on a cross-bot question it confabulates
member-bot constraints.

**Fix (decision):** inject identity on the path that already carries surface/authority —
1. `home_chat_routes.py` `session_ctx`: add `bot_id: "evolve"` + `is_admin_bot: true`.
2. `proxy.py` `format_session_context`: render bot identity into the `<session-context>` block.
3. `packages/analyzer/evolve_bot/AGENTS.md` surface-awareness section: teach that
   `admin_ui` ⇒ you ARE the admin/evolve bot, cross-bot tools available, the tray IS
   the admin-UI chat → never deflect; Rung 1.

**Deploy:** admin-ui code (routes/proxy) → admin-ui kickstart; AGENTS.md → repo-pull +
evo-gateway kickstart so the bot reloads its prompt. Canary-gated (`pod.release.mode=canary`).

## 7. Backlog (seed)

- Verify surface-aware-help-style is actually wired for the admin-UI tray (the Explore
  map suggests AGENTS.md teaches surface but identity is missing) — audit the other rungs.
- Screen-grounding: does the tray pass enough page state (e.g. the rendered "atlas ✗
  push failed 5×") for Evo to answer from the screen instead of re-fetching?
- A guard/test that the `<session-context>` always contains bot identity (regression lock).
