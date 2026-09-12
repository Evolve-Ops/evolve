# Application Apps

Application apps are installable extensions that add role-specific behaviors to
OpenClaw bots. They are separate from Evolve core, which benefits the whole pod.
An application app is installed on a specific bot to give it new skills.

Think of Evolve as the operating model layer and application apps as the plugins.

---

## Evolve core vs application apps

| | Evolve core | Application app |
|---|---|---|
| Who benefits | Whole pod | One specific bot |
| Where it lives | `/Users/Shared/evolve-repo/` | `applications/<name>/` |
| How it's installed | `evolve-admin deploy` | `evolve-admin application install` |
| Examples | Health scoring, proposals, cost alerts | Morning brief, meeting prep, CRM |

---

## How to install an application app

```bash
# Dry run — shows what would happen
evolve-admin application install ea-pack --bot admin-bot

# Proceed with installation
evolve-admin application install ea-pack --bot admin-bot --confirm
```

The install command will:
1. Read the app's `APPLICATION.md` manifest
2. Check that required integrations and tools are available
3. Copy scripts to the bot's workspace (or a shared location)
4. Register any cron schedules as launchd jobs
5. Append system prompt additions to the bot's config
6. Log the installation to `/Users/Shared/evolve/applications/{bot}/installed.json`

---

## How to build an application app

Start from the template:

```bash
cp -r applications/template applications/my-app
```

Then edit:

1. **`APPLICATION.md`** — define what your app does, what it needs, how to configure it
2. **`scripts/`** — add Python scripts for crons or analyzers
3. **`prompts/`** — add system prompt additions (plain markdown, appended to bot system prompt)
4. **`install.sh`** — add any one-time setup steps (create dirs, set permissions, etc.)

### APPLICATION.md fields

| Field | Required | Description |
|---|---|---|
| `name` | yes | Human-readable name |
| `version` | yes | Semver |
| `description` | yes | One paragraph |
| `requirements.integrations` | no | External services needed (Gmail, Calendar, etc.) |
| `requirements.tools` | no | OpenClaw tools needed (web_search, etc.) |
| `requirements.memory` | no | Whether persistent memory files are used |
| `requirements.schedule` | no | Cron schedule(s) for scripts |
| `what_it_adds` | yes | Bullet list of new behaviors |
| `config` | no | JSON config block with defaults |
| `compatible_roles` | yes | Which bot roles can use this app |
| `not_compatible_with` | no | Roles or apps that conflict |

---

## Priority list of planned application apps

| Priority | App | Description |
|---|---|---|
| 1 | **ea-pack** | Executive assistant: morning brief, meeting prep, task sweep, commitment tracking |
| 2 | **health-tracker** | Daily nutrition log, supplement reminders, lab result parsing |
| 3 | **crm-lite** | Contact relationship tracking, follow-up reminders, interaction history |
| 4 | **newsletter-curator** | Weekly digest of saved articles and RSS feeds |
| 5 | **meeting-prep** | Pre-meeting brief from calendar + CRM data |
| 6 | **journal** | Daily end-of-day journal prompt and monthly summary |
| 7 | **expense-tracker** | Receipt parsing, category tagging, monthly spend summary |
| 8 | **home-manager** | Home repairs tracker, vendor contacts, maintenance schedule |
| 9 | **travel-planner** | Trip planning assistant with packing lists and itinerary |
| 10 | **fundraise-manager** | Investor pipeline, update drafting, commitment tracking |

---

## Directory layout

```
applications/
├── README.md               ← this file
├── template/               ← starter template for new apps
│   ├── APPLICATION.md       ← manifest (copy and edit)
│   ├── scripts/            ← put Python scripts here
│   ├── prompts/            ← put system prompt additions here
│   └── install.sh          ← one-time setup steps
└── ea-pack/                ← Executive Assistant application app
    ├── APPLICATION.md
    ├── scripts/
    │   └── morning_brief.py
    └── prompts/
        └── ea-system-prompt.md
```
