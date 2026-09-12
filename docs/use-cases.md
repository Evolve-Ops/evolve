# OpenClaw Use Cases — Community Repository

*A living document of how people actually use OpenClaw bots.*
*Maintained as a reference for building Evolve Application Apps.*

Last updated: 2026-04-07

---

## How to Read This Document

Each use case is tagged with:
- **Complexity:** Simple / Moderate / Complex
- **Requires:** what integrations/tools are needed
- **Application App?** whether this warrants a dedicated Evolve application pack
- **Source:** where we found this (community, observed, research)

---

## Personal Productivity

### Daily Briefing
Morning summary of priorities, calendar, overdue tasks, email highlights.
- Complexity: Moderate
- Requires: Gmail, Google Calendar, task manager (Todoist/etc.)
- Application App: Yes — "Morning Brief" cap
- Source: Sarver article, community consensus

### Email Triage
Inbox summary, priority flagging, draft replies in owner's voice.
- Complexity: Moderate
- Requires: Gmail/Outlook API
- Application App: Yes — "Email Manager" cap
- Source: openclawdesktop.com research, community

### Calendar Management
Scheduling, availability lookup, meeting creation, conflict detection.
- Complexity: Simple
- Requires: Google Calendar API
- Application App: Part of "Personal Assistant" cap
- Source: Community, observed (Admin-bot)

### Reminders and Task Management
Create, track, prioritize tasks. Rolling forward detection. Deadline alerts.
- Complexity: Simple
- Requires: Task manager API (Todoist, etc.) or flat markdown
- Application App: Part of "Personal Assistant" cap
- Source: Community (r/openclaw), observed (Admin-bot tasks.md)

### Research Assistant
Web research, summarization, saving findings to memory files.
- Complexity: Simple
- Requires: Brave Search or Perplexity
- Application App: No — this is core OC behavior, no cap needed
- Source: Universal

### Travel Planning
Parse booking confirmations, build itineraries, flag gaps, set reminders.
- Complexity: Moderate
- Requires: Gmail (to read confirmations), calendar
- Application App: Yes — "Travel Assistant" cap
- Source: Sarver article, community

### Expense Tracking
Auto-pull receipts from email, categorize, track against budget.
- Complexity: Moderate
- Requires: Gmail, Google Sheets
- Application App: Yes — "Finance Tracker" cap
- Source: Sarver article

---

## Professional / Business

### Executive Assistant / Chief of Staff
Full operating model: email, calendar, meeting prep, follow-through, task sweep, relationship tracking.
- Complexity: Complex
- Requires: Gmail, Calendar, note-taker API (Granola/etc.), task manager
- Application App: Yes — "EA Pack" (premium, multi-component)
- Source: Sarver article (Stella), Ryan Carson post, community

### Meeting Prep Brief
60-min pre-meeting brief: prior notes on attendees, email threads, open items, pipeline stage.
- Complexity: Moderate
- Requires: Memory, Gmail, Calendar
- Application App: Yes — part of "EA Pack"
- Source: Sarver article

### Post-Meeting Processing
Fetch notes from Granola/note-taker, extract action items, route tasks, track commitments per person.
- Complexity: Moderate
- Requires: Note-taker API, task manager
- Application App: Yes — part of "EA Pack"
- Source: Sarver article

### CRM / Relationship Tracking
Persistent context on contacts: last touchpoint, commitments, what they care about.
- Complexity: Moderate
- Requires: Memory (flat markdown), email
- Application App: Yes — "Relationship Manager" cap
- Source: Sarver article, community

### Fundraising Pipeline (VC/Startup)
LP/investor contact tracking, deck version history, question tracking, commitment follow-through.
- Complexity: Complex
- Requires: Memory, Gmail, spreadsheet
- Application App: Yes — "Fundraise Manager" cap
- Source: Sarver article (explicit use case)

### Lead Research / Outreach
Automated lead research on schedule, personalized draft outreach.
- Complexity: Complex
- Requires: Web search, Gmail
- Application App: Yes — "Lead Research" cap
- Source: Reddit r/clawdbot (explicitly mentioned)

### Content Calendar / Blog Publishing
Draft blog posts, schedule, publish to CMS.
- Complexity: Moderate
- Requires: Web search, CMS API
- Application App: Yes — "Content Manager" cap
- Source: o-mega.ai research

### Customer Support (Small Business)
Respond to common inquiries, escalate complex issues, log tickets.
- Complexity: Moderate
- Requires: Email/messaging channel, knowledge base
- Application App: Yes — "Support Bot" cap
- Source: Community, remoteopenclaw.com

---

## Developer / Technical

### Automated Bug Fix Pipeline
Nightly cron: pull latest bugs from issue tracker, write tests, fix, deploy.
- Complexity: Complex
- Requires: GitHub API, BugSnag or similar
- Application App: Yes — "DevOps Bot" cap
- Source: r/ClaudeCode (explicitly described)

### Code Review Assistant
Review PRs, flag issues, suggest improvements.
- Complexity: Moderate
- Requires: GitHub API
- Application App: Yes — part of "DevOps Bot" cap
- Source: Community, observed (agents used for Evolve development)

### Multi-step Deploy Pipeline
Patch + test + deploy pipelines with human approval checkpoints.
- Complexity: Complex
- Requires: GitHub, CI/CD system
- Application App: Yes — "Deploy Bot" cap
- Source: r/clawdbot

### API Documentation Generator
Browse a site's network requests, generate API docs, output to backend.
- Complexity: Moderate
- Requires: Browser tool, web fetch
- Application App: No — core OC tool use, no cap needed
- Source: r/openclaw community

### Self-Evolving Codebase (Evolve itself)
Meta use case: OC bot manages its own codebase, proposes improvements, runs agents to implement them.
- Complexity: Complex
- Requires: GitHub, coding agent (Codex/Claude Code), memory
- Application App: Evolve itself IS this application
- Source: Observed (this project)

---

## Home & Lifestyle

### Home Automation
Control smart home devices via Home Assistant, set up integrations, automate routines.
- Complexity: Moderate to Complex
- Requires: Home Assistant, local network access
- Application App: Yes — "Home Controller" cap
- Source: Multiple Reddit threads (very popular use case)

### Smart Home Setup Assistant
Set up Home Assistant from scratch: Docker containers, integrations, static IPs, device discovery.
- Complexity: Complex
- Requires: Exec tool, local network
- Application App: Yes — "Home Setup Wizard" cap
- Source: r/openclaw ("holy shit" moment — set up HA, discovered Sonos speakers, mapped rooms)

### Grocery List / Meal Planning
Track pantry, suggest meals, generate shopping lists, order groceries.
- Complexity: Simple
- Requires: Web search, optional: grocery delivery API
- Application App: Yes — "Kitchen Assistant" cap
- Source: r/openclaw ("I run mine as a daily assistant... even grocery lists")

### Daily Journal / Life Logging
Capture daily notes, reflections, mood, activities. Synthesize weekly summaries.
- Complexity: Simple
- Requires: Memory (flat markdown)
- Application App: Yes — "Life Logger" cap
- Source: Community

### Health & Fitness Tracking
Track workouts, nutrition, medications, supplements, sleep (via Whoop/etc.)
- Complexity: Moderate
- Requires: Health API (Whoop, Apple Health export, etc.), memory
- Application App: Yes — "Health Tracker" cap
- Source: Observed (Admin-bot health tracking for Pod-admin)

### Language Learning
Daily vocabulary, grammar exercises, conversation practice.
- Complexity: Simple
- Requires: Core LLM only
- Application App: Yes — "Language Tutor" cap
- Source: Community

---

## Automated / Scheduled Jobs

### Flight Check-In
Automatic web check-in when flights open (24h before).
- Complexity: Moderate
- Requires: Browser tool, calendar (to know about flights)
- Application App: Yes — part of "Travel Assistant" cap
- Source: Community consensus (frequently mentioned)

### Site Monitoring / Price Alerts
Monitor a website for changes, alert when price drops or stock appears.
- Complexity: Moderate
- Requires: Web fetch, scheduled cron
- Application App: Yes — "Monitor Bot" cap
- Source: Community

### Newsletter / Digest Curation
Weekly curated digest from tracked sources, scored by relevance.
- Complexity: Moderate
- Requires: Web search, RSS feeds
- Application App: Yes — "Intelligence Digest" cap
- Source: Sarver article (weekly VC/founder/operator digest)

### Social Media Monitoring
Track mentions, surface important posts from followed accounts.
- Complexity: Moderate
- Requires: Twitter/X API or web fetch
- Application App: Yes — "Social Monitor" cap
- Source: Community

### Automated Relationship Maintenance
Periodic "reach out to this contact" reminders with drafted messages.
- Complexity: Moderate
- Requires: Memory (contact list), messaging
- Application App: Yes — part of "Relationship Manager" cap
- Source: Community

---

## Education & Research

### Research Paper Summarizer
Fetch, summarize, and file academic papers to a research library.
- Complexity: Simple
- Requires: Web fetch, memory
- Application App: No — core OC behavior
- Source: Community

### Competitive Intelligence
Track competitors, surface news, summarize product updates.
- Complexity: Moderate
- Requires: Web search, scheduled cron
- Application App: Yes — "Competitive Intel" cap
- Source: Community, business use cases

### Learning Assistant
Custom tutoring, spaced repetition, knowledge testing.
- Complexity: Moderate
- Requires: Core LLM, memory
- Application App: Yes — "Learning Bot" cap
- Source: Community

---

## Multi-Bot / Specialized Deployments

### Primary + Specialist Bots
One primary personal assistant + specialized bots for specific domains (coding, home, business).
- Complexity: Complex
- Requires: Multiple OC instances, Evolve pod
- Application App: Evolve pod architecture enables this
- Source: Observed (Admin-bot + Team-bot-a + Team-bot-c setup)

### Freelancing / Agent Marketplace
Multiple agents competing on tasks, businesses select best result.
- Complexity: Complex
- Requires: Multi-agent coordination
- Application App: Specialized — not in standard Evolve scope
- Source: r/openclaw (3,000 agents, 30+ competing per task)

### Family Assistant
Shared bot for household (multiple users), calendar coordination, shared shopping lists.
- Complexity: Moderate
- Requires: Multi-user session isolation, shared memory
- Application App: Yes — "Family Hub" cap
- Source: Community

---

## Observed Use Cases (Our Setup)

| Bot | Primary Use Case |
|---|---|
| Admin-bot | Personal assistant — health, travel, home, tasks |
| Team-bot-a | Example Corp project manager — PROJECT-X coordination, team management |
| Team-bot-c | Additional capacity / experiments |
| Personal-bot-user (Team-bot-b) | Disney DCP project "The Real Story" |

---

## Application App Priority List

Based on frequency in community + our own needs, priority order for building:

1. **EA Pack** — daily brief, email, meeting prep/follow-through, task sweep (Sarver)
2. **Home Controller** — Home Assistant integration (very popular community use case)
3. **Travel Assistant** — itinerary, flight check-in, confirmations
4. **Health Tracker** — metrics, medications, supplements
5. **Intelligence Digest** — curated weekly digest from tracked sources
6. **Relationship Manager** — contact context, commitment tracking
7. **DevOps Bot** — GitHub, CI/CD, bug fix pipeline
8. **Monitor Bot** — site monitoring, price alerts
9. **Family Hub** — multi-user household assistant
10. **Fundraise Manager** — LP pipeline (niche but high value)
