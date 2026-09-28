---
name: evolve-knowledge
description: >
  Answer operator questions about Evolve itself — what a page does, how a
  concept works, what a setting means, where something moved. Reads from
  the canonical help corpus at docs/help/ instead of guessing from training
  data. Use whenever the operator asks "what is …", "how does … work",
  "where is …", or any other question about how Evolve as a product
  behaves.
metadata:
  evolve:
    authored_by: evolve
    authored_at: "2026-06-05T00:00:00Z"
    # No obviated_by — this skill grounds answers in a maintained corpus
    # rather than filling a tool gap.
---

# Answer questions about Evolve from the help corpus

When the operator asks anything about how Evolve works — a page, a concept,
a setting, a workflow, the difference between two things — do NOT answer
from general knowledge. Answer **only** from the help corpus at
`/Users/Shared/evolve-repo/docs/help/`. That corpus is the source of truth
maintained by a weekly knowledge-refresh PR; anything outside it is
unverified.

## When this skill applies

Trigger on any question whose answer is "how the product works":

- "What does the Recommendations page show?"
- "How do I add a bot?"
- "Where did the Channels tab go?"
- "What's the difference between a proposal and a signal?"
- "How does the User Profile Inferrer decide what to record?"

Do NOT use this skill for:

- Diagnosing a live problem on this pod (use `pod_state.*` tools instead).
- Editing config or applying proposals (those have their own actions).
- Strategy or opinion ("should I use X?") — defer to the operator.
- Questions about the broader OpenClaw ecosystem or other AI tools — those
  aren't in this corpus.

## Recipe

1. **Locate the relevant file(s).** Read
   `/Users/Shared/evolve-repo/docs/help/_index.yaml`. It maps concepts to
   the files that own them. Tokenize the operator's question (lowercase,
   ignore stopwords), match tokens against concept keys, and pick the
   top 1-3 owning files by hit count.

   If no concept matches with at least one token hit, the corpus does not
   cover the question — see step 4.

2. **Read those files in full.** They're short markdown with YAML
   frontmatter — read the bodies (skip the frontmatter when composing your
   answer; it's metadata for the corpus, not user-facing content).

3. **Answer in the operator's voice.**

   - Lead with the direct answer (1-2 sentences).
   - Add 2-4 bullets of supporting detail only if the question warrants it.
   - End with a `Sources:` line citing the help-file paths you read,
     formatted as relative paths (e.g. `docs/help/recommendations.md`).
   - Do NOT invent details. If the corpus says X and the operator asks
     about Y, say "I don't see Y covered — the closest is X."

4. **When the corpus doesn't cover the question.** Say so honestly:

   > I don't see this covered in the Evolve help corpus. Want me to flag
   > it as a docs gap so the weekly knowledge-refresh routine picks it up?

   Do NOT fall back to general knowledge. A confident wrong answer about
   how Evolve works is worse than an honest "not covered."

## Why this is constrained

The help corpus is what backs both the public help site and the
community-facing research bot (Atlas). If evo answers from training data
and Atlas answers from the corpus, the same question gets two different
answers — and the operator loses trust in both. Grounding every "how does
Evolve work" answer in the same corpus is what keeps the surfaces
consistent.

The corpus is refreshed weekly via a scheduled docs(help) PR. If
something feels stale, the right action is to flag the gap (step 4),
not to improvise.

## Notes on the corpus shape

- One file per admin UI page, plus a few orientation files
  (`overview.md`, `getting-started.md`, `quick-start.md`) and per-system
  explainers (`profile-inferrer.md`, `continuity.md`).
- Every file has YAML frontmatter with `concepts:` listing what it owns.
  The `_index.yaml` controlled vocabulary is the joining table.
- `meta-health.md` is deprecated (its content moved to Recommendations +
  Alerts) — its frontmatter has `status: deprecated`. Use it only to
  redirect the operator to the new home.
- Internal specs, audits, diagnoses, and incident reports under `docs/`
  are NOT in the corpus and NOT a fallback. They are not public-quality
  content and must not be quoted.
