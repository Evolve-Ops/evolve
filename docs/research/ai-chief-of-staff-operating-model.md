# AI Chief of Staff: An Operating Model

**Source:** Ryan Sarver (@rsarver) on X — https://x.com/rsarver/status/2041148425366843500  
**Archived:** 2026-04-13

---

After meeting: processes through Granola API (any note-taker with API). Fetches notes, deduplicates, extracts action items. Owner's tasks → Todoist. Other people's commitments → per-person markdown files (one per person). If you asked someone to do something 3 weeks ago and no update — system knows.

## Operational rhythm

- Morning brief (9am): top priorities, overdue tasks, today's calendar, anything needing attention before opening laptop
- Evening wrap (6pm): what happened, what stalled, what to prep for tomorrow
- Both via WhatsApp. If nothing to say, silence.

> "This is the piece that makes it feel like working with a chief of staff rather than using a tool."

## Kaizen: the system improves itself

Every Friday: cron job runs research. Scans OC community, checks for new patterns, looks at what other builders are doing, saves to memory/kaizen-research-YYYY-MM-DD.md.

Sunday morning: review together. Summarizes week's research, surfaces top ideas worth trying, discuss what to actually change.

Also learns from daily interactions. If something keeps getting corrected, or a feature creates more friction than value — captured in memory, eventually surfaces as a suggestion to fix.

> "A human chief of staff genuinely cannot do this at scale. They can learn from working with you, but they can't simultaneously scan what hundreds of other builders are doing and cross-reference it against your system every week."

Drives continuous refactoring: build feature → run for weeks → see how it fits → clean up or cut. First versions always too complicated, too noisy, or solving wrong problem. Smaller system you trust > bigger system you route around.

## Layer separation rule (critical design principle)

LLMs handle judgment. Scripts handle everything else.

- Deterministic (reading files, calling APIs, sending messages, comparing timestamps) → Python
- Synthesis, prioritization, drafting, reasoning → LLM

> "When you push deterministic work through an LLM, things break in unpredictable ways and you stop trusting the system."

## Key quote

> "I didn't get the best assistant I've ever had by asking better questions. I got it by giving the system a better operating model."
