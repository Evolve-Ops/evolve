# Fixture pod — a stranger's multi-bot OpenClaw pod, on disk

A three-bot pod with months of plausible history, built into a directory you
choose, that the **real** admin server, application scanner and analyzer read
with their own code. It exists because there was no other way to stand one up:
`tests/browser/fixtures/network.json` describes a pod with zero bots, every
populated Apps test stubs the HTTP reads, and real bot homes need real accounts,
which need root. So the journey that the alpha gate turns on — *install onto an
existing multi-bot pod and look at it* — could not be rehearsed anywhere but a
live pod.

Written for `internal/audit-alpha-journey-2026-08.md`; kept because demo
rehearsals, screenshots and journey regressions all need the same thing.

## Use it

```bash
ROOT=/tmp/fixture-pod

# 1. Build the pod. Three bots, 214 / 151 / 96 days of history, empty manifests
#    (Evolve has never been here).  --age-days N shifts the history back.
python3 -m tests.fixtures.pod.build --root "$ROOT"

# 2. Serve the real admin UI over it.
python3 -m tests.fixtures.pod.serve --root "$ROOT" --port 5099

# 3. Optional: the drafts a working discovery scan would have written.
python3 tests/fixtures/pod/seed_drafts.py --root "$ROOT"

# 4. Optional: a week of post-install turn annotations + session summaries, and
#    the real per-app + per-user usage rollups over them.
python3 tests/fixtures/pod/annotate.py --root "$ROOT" --days 7

# 5. Optional: screenshots, both themes.
python3 tests/fixtures/pod/shots.py --base-url http://127.0.0.1:5099 \
    --out /tmp/shots --tag pod
```

Steps 3–5 need the fixture environment (`serve.fixture_env` builds it); the
simplest way to get it is to run them with the same `EVOLVE_FIXTURE_POD_ROOT`
and `PYTHONPATH` `serve.py` prints when it starts.

> `python -m tests.fixtures.pod.<mod>` resolves only when `packages/admin` is not
> ahead of the repo root on `PYTHONPATH` — `packages/admin/tests/` shadows the
> `tests` package. `seed_drafts.py` and `annotate.py` are therefore also runnable
> as plain script paths, which is what the commands above do.

## What is real and what is not

**Real:** every path, every file, and every reader. Bot workspaces, OpenClaw
cron stores, turn-collector history, the shared dir, `network.json`. The admin
server resolves bot homes, lists manifests, hashes files and rolls up usage with
its own code against these files.

The cron stores cover **both** OpenClaw backends on purpose, because both are
in the wild: `personal-bot` and `team-bot-a` are on a pre-2026.7 gateway and
keep their jobs in `cron/jobs.json`; `admin-bot` is on ≥2026.7, so the gateway
imported the seed into `state/openclaw.sqlite` (table `cron_jobs`) and left
`cron/jobs.json.migrated` behind. Payload kinds are OpenClaw's own — `command`,
`agentTurn`, `systemEvent` — and one job on the personal bot is switched off,
so a reader that counts it is counting a schedule that cannot fire.

**Not real — three things, all of them named at the point of use:**

1. **Where the homes are.** `sitecustomize.py` pins
   `platform_profile.set_profile` at the fixture root (the product's own
   documented seam) and answers `pwd.getpwnam` for the three fixture bot ids.
   Nothing else is redirected; all path math is still the product's.
   `EVOLVE_FIXTURE_POD_NO_PWD_SHIM=1` disables the second redirect.
2. **The discovery scan's Phase 2.** `seed_drafts.py` supplies the detections an
   LLM would have returned and hands them to the scanner's own `_stub_manifest`,
   `_stamp_discovered_files` and layer classifier. Manifests seeded this way are
   trustworthy as *shape* and as *presentation* and say nothing about what a real
   scan would find — **never quote a timing or recall number from them.**
3. **Post-install attribution.** `annotate.py` writes `turn_annotation` rows with
   `app_attribution: "scheduled"`, which on a real pod needs a cron→app join that
   only the app-install path writes. It shows the *best* case on purpose.

There is no `openclaw` binary and no gateway behind these homes, so anything that
shells out (`crontab -l`, `openclaw cron list`, `sudo`) gets the same empty answer
it would get from a stopped bot. Surfaces that read the store *on disk* — the
application scanner's cron inventory, since ALPHA-6 — see the schedules anyway,
which is the point: a stopped bot still has habits.

## Safety

`build.fixture_bot_ids_are_safe()` refuses to build if any fixture bot id names a
real account on the host — otherwise `bot_home()` would resolve through `pwd` to
that person's home and the harness would read and write it. `build()` removes and
recreates the root it is given, so point it somewhere disposable.

Bot ids are role placeholders per `docs/PLACEHOLDER_NAMING.md`.
