# Placeholder Naming Convention

This project is developed against a private reference deployment whose bot and account names are deployment-specific. Tracked code, tests, and docs MUST use role-named placeholders instead of those deployment-specific names so the codebase reads correctly on any install.

The `test_public_launch_scrub.py` invariant test enforces this. If you hit a CI failure citing this doc, the table below tells you the substitution to make.

## Identifier mapping

### Personal / account identifiers

| Reserved (do not use) | Use instead |
|---|---|
| Personal first/last names of the maintainer | (omit; or use `pod-admin` in role contexts) |
| First names of the maintainer's family or close contacts | (omit; or use a role like `pod-admin-family-member`) |
| Reference admin macOS account name | `pod-admin-user` |
| Reference personal-bot macOS account name (one per personal-assistant bot) | `personal-bot-user` |

### Bot identifiers (reference deployment → role placeholder)

| Reserved bot identifier | Role placeholder |
|---|---|
| Slack team bot | `team-bot-a` |
| Admin / pod-ops runner bot | `admin-bot` |
| Discord team bot | `team-bot-b` |
| Generic team bot (test fixtures) | `team-bot-c` |
| Personal-assistant bot | `personal-bot` |
| Security-focused bot | `security-bot` |

The CI guard's reserved-token list is the source of truth — see `packages/admin/tests/test_public_launch_scrub.py::RESERVED_TOKENS`.

### Test fixtures

Where uniqueness matters more than role (e.g., parameterized fixtures), use generic `bot-a`, `bot-b`, `bot-c` etc.

### Public bot identities (NOT reserved)

The names `evo` and `evolve` are intentional public bot identities and are exempt from the scrub.

## Why this convention exists

The codebase was developed against a single reference deployment for the first months of its life. The first public-launch scrub (PR #706) substituted role names everywhere. A 2026-05-30 audit caught regressions where new code reintroduced deployment-specific names. The CI guard added in PR #1835 prevents that class of drift from recurring.

## Adding a new identifier to the reserved list

1. Update `RESERVED_TOKENS` in `packages/admin/tests/test_public_launch_scrub.py`.
2. Add a row to the appropriate table above.
3. Sweep existing occurrences in the same PR (the test will fail otherwise).
