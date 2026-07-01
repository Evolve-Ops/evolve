# Correspondence Persona — Spec

**Status:** draft (2026-05-30)
**Calibrated against:** the onboarding of an external-correspondence-heavy personal-assistant bot in late May 2026 — first concrete case where a bot's primary value comes from outbound vendor correspondence (travel: hotels, airlines, activity vendors) rather than internal pod automation.
**Companion docs:**
- [docs/spec-google-integration-paths-2026-05-30.md](spec-google-integration-paths-2026-05-30.md) — Gmail integration paths (consumes the persona's `email_address` and `signature` fields)
- [docs/spec-add-bot-wizard-2026-05-28.md](spec-add-bot-wizard-2026-05-28.md) — Wizard Screen 2 ("Bot identity") is where persona is collected
- [docs/spec-manifest-v7-2026-05-20.md](spec-manifest-v7-2026-05-20.md) — `audience_scoping{}` is the trust boundary that determines when persona vs internal identity is used
- [packages/admin/evolve_admin/provisioning.py](../packages/admin/evolve_admin/provisioning.py) — `display_name` field today (currently unused per the source comment)

This spec uses stock example names throughout: **`lex`** for the bot's internal identifier, **`Sam`** for the primary user, **`Jane`** for the persona. None of these refer to any real bot, user, or deployment.

---

## 0. Purpose

A bot has two audiences with different needs:

- **Internal audience** (the operator, the primary user, other bots in the pod, evo, the admin UI). They know it's a bot, they call it by its `bot_id` or `display_name`, and any disclosure norm is satisfied by the fact that they installed it.
- **External audience** (vendors, third-party correspondents, end recipients of emails the bot sends or messages it posts). They have no prior relationship with the bot. The bot's internal name may be opaque, confusing, or signal-laden in ways that hurt the correspondence's primary purpose.

This spec introduces a **correspondence persona**: a per-bot, audience-scoped presentation identity that the bot uses when communicating with external audiences. It is distinct from the bot's internal identity and from `display_name`, both of which remain unchanged.

The feature is generalizable across any external-facing channel (email today, Slack/Discord client-facing channels later) but the v1 implementation targets email because that is the blocking dependency for the bot that surfaced the need.

---

## 1. Why this spec exists

The motivating bot is a personal-assistant whose primary value comes from outbound correspondence to strangers — hotels, airlines, activity vendors, rental-car desks. Its operator and primary user picked an internal name that is a literary reference: fine for internal use, but two characteristics make a literary or whimsical name unfit as a vendor-facing identity:

1. **Uncommon as a human first name.** A hotel concierge receiving a message signed by a literary-reference name is more likely to flag it as automated and triage it lower than a message from a stock human-style first name. The internal-fun-name pattern is good for operator/user satisfaction but actively bad at the vendor surface.
2. **Doesn't carry the disclosure decision.** The internal name makes no claim about being a bot or a person; the disclosure happens entirely in the signature line. If we want the disclosure rule to be a deliberate, configurable thing — not a side-effect of name choice — we need a separate slot for it.

Generalizing: every persona-bot (multi-domain personal-assistant compartments, client-facing project bots, future Evolve installs that pair a whimsical internal name with vendor-facing roles) will face the same fork. Putting "what name appears in the From header" and "what disclosure marker appears in the signature" into one configurable block — with conservative defaults and a single enforcement path — is cheaper than re-deriving it per bot.

---

## 2. Concepts

Three distinct names exist after this spec lands. Existing code does not change; the persona is additive.

| Slot | Where it lives | Audience | Stock example |
|---|---|---|---|
| `bot_id` | `network.json` key, filesystem paths, process names, signal-store records | platform | `lex` |
| `display_name` | `network.json` `bots.<id>.display_name` | internal humans (admin UI, evo references, conduct docs) | `Lex` |
| `correspondence.name` | `network.json` `bots.<id>.correspondence.name` | **external audiences only** | `Jane` |

Rule: **`bot_id` and `display_name` are NEVER used in outbound external communication.** When an outbound message is composed and the audience is external, the persona name (and signature, address, disclosure marker) must come from the `correspondence` block. There is no fallback to `display_name` for external surfaces — if `correspondence` is absent, the bot must refuse external sends (see §7).

---

## 3. Schema

New block under `network.json` `bots.<bot_id>.correspondence`. All fields optional at the schema level; defaults filled in by the wizard, with validation rules in §7 catching the unsafe combinations.

```yaml
bots:
  lex:
    # ... existing fields ...
    correspondence:
      name: "Jane"
      # Optional. The presentation name in From headers, signatures, and any
      # external-facing greeting line. If omitted, the bot is configured as
      # internal-only and external sends are blocked (see §7).

      email_address: "jane@example-domain.com"
      # Optional. If set, must be a valid send-as address verified on the
      # bot's Workspace mailbox (Workspace alias, "Send mail as" alias, or
      # the mailbox itself). If omitted, defaults to the bot's primary
      # Workspace address (i.e. lex@example-domain.com).

      signature: |
        Jane
        Assistant to Sam
      # Optional. The signature block appended to outbound emails. Templated:
      # {primary_user_name}, {persona_name}, {disclosure_marker} are
      # substituted from elsewhere. See §5 for the default template by
      # disclosure level.

      disclosure: "soft"
      # Required if `name` is set. One of:
      #   "explicit" — signature contains an unambiguous AI/assistant marker
      #                (default for first-time operators; recommended for
      #                regulated-jurisdiction use)
      #   "soft"     — signature describes the role ("assistant to X") but
      #                does not explicitly say "AI" / "automated"
      #   "none"     — no disclosure marker (NOT a default; requires explicit
      #                operator override AND a recorded justification, see §6)

      disclosure_override_reason: null
      # Required only when disclosure == "none". Free text. Recorded in the
      # bot's audit log and surfaced in the admin UI on the bot's tile so
      # the operator sees the override is in place.

      avatar: null
      # Optional URL or local path. Used as the visual identity in any
      # channel where personas have avatars (e.g. Slack profile, Gravatar).
      # Out of scope for v1; reserved field.
```

The schema lives next to existing per-bot config in `network.json`. It is read by the persona helper (§4) and validated by the wizard finalize step (§7).

---

## 4. Persona helper module

New module: `packages/admin/evolve_admin/persona.py` (admin side) plus a peer in the bot-side runtime (location TBD; likely a small skill-side helper in OpenClaw skill code that talks to Evolve's MCP layer).

Two public entry points:

```python
def resolve_persona(bot_id: str, audience: str) -> Persona | None:
    """Return the persona configured for this bot when speaking to
    the given audience. Returns None if the audience is INTERNAL
    (caller should use display_name / bot_id). Raises ValueError if
    the audience is EXTERNAL and the bot has no persona configured."""
```

```python
def build_from_header(persona: Persona, default_address: str) -> str:
    """Produce the RFC-5322 From header for an outbound email:
    '"Jane" <jane@example-domain.com>'. Uses persona.email_address if
    set, otherwise default_address. Encodes the display name correctly
    (RFC 2047 if non-ASCII)."""
```

Audience strings are aligned with `audience_scoping{}` from schema-v7 — see §7.

The helper has no Gmail-specific code. The Gmail integration spec ([spec-google-integration-paths-2026-05-30.md](spec-google-integration-paths-2026-05-30.md)) is the consumer; future Slack/Discord client-facing channels can call the same helper.

---

## 5. Default signature templates

The wizard supplies a default `signature` based on the chosen `disclosure` level. Operators can edit; the wizard records that edit as an intentional override.

| disclosure | Default template |
|---|---|
| `explicit` | `{persona_name}\nAI assistant to {primary_user_name}` |
| `soft`     | `{persona_name}\nAssistant to {primary_user_name}` |
| `none`     | `{persona_name}` (no role line, no disclosure marker) |

Substitution variables resolved at send time:
- `{persona_name}` — `correspondence.name`
- `{primary_user_name}` — `primary_user.name` from `network.json`
- `{disclosure_marker}` — `"AI assistant"`, `"assistant"`, or `""` per disclosure level

The `none` template is intentionally bare. If the operator wants a persona-with-no-disclosure, the signature is just the persona's name; we do not invent a misleading role line on the bot's behalf.

---

## 6. Disclosure policy

This is the load-bearing decision of the spec, not the schema. Three rules.

**Rule 1: Default is `soft`, not `none`.** When the wizard creates a persona, the disclosure level defaults to `soft`. The operator can change it to `explicit` (more disclosure) freely. Changing it to `none` requires answering an additional prompt — "Why?" — whose answer is recorded as `disclosure_override_reason` and surfaced on the bot's admin UI tile.

**Rule 2: The bot must answer truthfully if asked directly.** Regardless of `disclosure` level, every persona-bot has a hard rule in its `POD_CONDUCT.md` addendum:

> If a correspondent asks whether you are a human or an AI/automated system, or asks who you are, or otherwise makes clear they want to know the nature of the entity they are communicating with, you must answer truthfully. You are an AI assistant to {primary_user_name}. The disclosure-level config controls proactive signaling in the signature; it does not authorize lying.

This rule is enforced via conduct injection (the existing POD_CONDUCT → session_surface → systemAppend path). The bot's session sees the rule on every turn that involves an external audience.

**Rule 3: `disclosure: "none"` is a yellow flag, not a red one.** There are legitimate uses — e.g. a personal-assistant bot whose human user has long-standing relationships with vendors who would find a disclosure marker more confusing than informative. The wizard does not prevent it; it just requires the operator to record the reason, makes the choice visible in the admin UI, and makes Rule 2 (truthful-if-asked) non-overridable. California SB 1001, EU AI Act, and similar emerging regulations remain the operator's responsibility — the spec does not attempt to enforce specific jurisdictions, but the defaults bias toward compliance.

---

## 7. Audience scoping — when persona applies

Persona is used when and only when the audience is external. "External" is determined per-action via the existing `audience_scoping{}` field from schema-v7.

| `audience_scoping.operator` value | Persona used? |
|---|---|
| `operator_only` | No — internal audience |
| `named_users` | No — known internal users |
| `open` | **Yes** — external audience |

For email specifically (the v1 consumer), the audience determination is simpler: every outbound email is treated as external unless the recipient address resolves to a user known in the bot's `audience_scoping.role_capabilities` block. The Gmail integration spec covers this in detail.

Validation rule enforced by the wizard at finalize:

- If the bot has any application or capability whose `audience_scoping.operator == "open"` AND the bot does not have a `correspondence.name`, the wizard fails with a clear error pointing the operator to this spec. This prevents shipping an external-facing bot with no persona configured.

---

## 8. Display rules across surfaces

Where the bot's name appears in the UI today, only the internal name shows. Persona is purely external. The admin UI tile may surface persona as a metadata row but never replaces the bot identity.

| Surface | Shows | Notes |
|---|---|---|
| Admin UI bot tiles | `display_name` | Persona may appear as a small "Corresponds as: {persona_name}" row when configured |
| Admin UI bot detail page | `display_name`, with `correspondence` block as a section | Reveals persona + disclosure level + override reason |
| evo references to bots | `display_name` | "Lex sent 4 emails today" — never "Jane sent 4 emails today" |
| POD_CONDUCT.md | `bot_id` / `display_name` | Persona referenced only in the bot's own conduct addendum |
| RUNTIME_NOTES.md | `bot_id` | No persona references; runtime notes are internal-only |
| Signal store records | `bot_id` | Persona never appears |
| Outbound emails | `correspondence.name` + signature | Persona-only |
| Internal logs / audit | `bot_id` + persona name as metadata | Both, with persona explicitly labeled to avoid confusion |

The principle is: **internal identity is the spine; persona is the costume the bot wears when it leaves the house.**

---

## 9. Defaults and absent-config behavior

| Bot type | Default behavior at deploy |
|---|---|
| New bot via wizard, no external surfaces | No persona configured. Wizard does not prompt for persona. Bot cannot send external email; tries to do so are rejected by the Gmail integration (see companion spec). |
| New bot via wizard, has external surfaces (e.g. `audience_scoping.operator == "open"`) | Wizard prompts for persona on Screen 2 (Bot identity). Pre-fills `name` with `display_name`, `disclosure` with `soft`, `email_address` blank (uses mailbox default), `signature` with the soft template. Operator can edit or accept. |
| Existing bot with no `correspondence` block | No change. The persona field is additive; existing bots continue as-is. If they later need external-facing capabilities, the migration is a one-time wizard re-run on the bot. |
| Existing bot that already sends external email | Should be migrated to a persona block as part of the broader Gmail integration migration. Not blocking this spec; tracked as a follow-up. |

---

## 10. Open questions

1. **Per-channel personas.** Does an operator want a bot to use one persona in email but a different one in another channel? For v1 we assume one persona per bot; if multi-channel divergence becomes a real need, add `correspondence.by_channel: {email: {...}, slack: {...}}` later. The schema is forward-compatible (the v1 top-level fields can become defaults for unspecified channels).

2. **Persona name as PII.** `correspondence.name` may be a real first name. Today `network.json` lives at `/Users/Shared/evolve/network.json` (mode 0644, world-readable to anyone with mini access) — that's fine for `bot_id` and ports but is the wrong store for what is effectively a piece of personal data. Options: move persona block to per-bot config (e.g. `bots/<id>/persona.yaml` with stricter ACL); accept the current location given the deployment's small user set; or split network.json so the sensitive parts have separate ACL. Recommend deciding when the Gmail integration spec lands, since secrets-store decisions there inform this one.

3. **Scrub-guard for persona names.** A stock name like "Jane" is generic enough to not warrant being added to `RESERVED_TOKENS`. But the *general case* — a persona name that is a real human first name — would warrant guard coverage if it leaked into tracked test fixtures. Recommend treating persona names like other personal identifiers: they should not be hardcoded into tracked code, but the scrub-guard list isn't the right enforcement (the persona values come from runtime config, not source). The right check is a separate test that asserts no bot's `correspondence.name` value appears in tracked code.

4. **Signature changes propagation.** When the operator changes `signature` in the admin UI, does the change take effect on the bot's next session or immediately? Current `network.json` sync semantics suggest "next session." This is fine for v1; flag if it becomes a friction point.

5. **What happens if the persona is changed after the bot has corresponded with a vendor as the old persona?** Vendors will see two senders for the same underlying mailbox. Out of scope for v1; document the consequence in the admin UI when the operator goes to change a persona, but do not block.

---

## 11. Out of scope

- Multi-persona per bot (one bot wearing multiple correspondence identities for different external audiences). Forward-compatible schema, deferred implementation.
- Per-channel persona variation. Forward-compatible, deferred.
- Persona-as-DKIM-domain configuration. Email deliverability concerns covered by the Gmail integration spec, not here.
- Persona-as-Brave-MCP-tool-name or other tool-surface impersonation. Out of scope — personas are for human-facing correspondence, not tool wrappers.
- Per-vendor persona memory ("Jane already corresponded with this vendor desk; use the same persona next time"). State-management problem; defer.

---

## 12. PR plan

Three PRs, each small and independently shippable.

| PR | Scope | Files |
|---|---|---|
| α | Schema + persona helper + validation. No UI, no behavior change for existing bots. | `network.json` schema doc, `persona.py` helper, validation hook in `provisioning.py` finalize step, unit tests |
| β | Wizard Screen 2 persona collection. Pre-fills, edit-in-place, default template selection. | `wizard_routes.py`, `web/index.html` Screen 2 section, integration test |
| γ | Admin UI display surfaces (bot tile metadata row, bot detail page persona section), POD_CONDUCT auto-injection of Rule 2 for any bot with a persona. | UI templates, conduct-injection hook |

PR α is non-blocking for the v1 case-study bot's onboarding (the helper can be called by the Gmail integration spec's PR even before the wizard supports it). PRs β + γ make the feature operator-facing.

---

## 13. References

- External: California SB 1001 (Bolstering Online Transparency Act) — bot disclosure in commercial contexts
- External: EU AI Act Article 50 — transparency obligations for AI systems interacting with humans
- Internal memory: voice/tone targets, Plex-test design constraint, three-user-types model, and POD_CONDUCT injection mechanism inform the wizard UX and the conduct-injection layer for Rule 2.
