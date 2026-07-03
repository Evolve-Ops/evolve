# Help-site build — `docs/help/` → `docs/gitpages/help/`

A small Python script that renders the help corpus to static HTML matching
the existing landing-page visual identity.

## Run it

```bash
pip install markdown pyyaml      # build-time only
python3 tools/build_help_site.py
```

Output lands in `docs/gitpages/help/`:

```
index.html              ← generated index grouped by section
<slug>.html             ← one per docs/help/<slug>.md (audience: public)
_assets/help.css        ← stylesheet matching docs/gitpages/index.html
_assets/help.js         ← in-page search
```

The build is deterministic — same inputs produce byte-identical outputs.
Safe to commit the output and rebuild in CI as a consistency check.

## Local preview

```bash
python3 -m http.server 7050 --directory docs/gitpages
# then open http://localhost:7050/help/
```

`.claude/launch.json` already declares this server as `gitpages`, so the
Claude Preview MCP picks it up automatically.

## Deploying via GitHub Pages

Two options. Both rebuild on push to `main`; pick one.

### Option A — commit the built output (simplest)

Run the build locally before merging changes to `docs/help/*.md` and
commit `docs/gitpages/help/` alongside the source edits. GitHub Pages
serves directly from `docs/gitpages/` with no Action required.

A CI check ([.github/workflows/help-site-check.yml](../.github/workflows/help-site-check.yml))
rebuilds on every PR and fails if the committed output differs from a
fresh build — so the source and the rendered site can't drift.

### Option B — build in CI, skip committing

Use the official `actions/upload-pages-artifact` +
`actions/deploy-pages` flow. The build runs on every push to `main`
and the resulting artifact deploys to Pages without polluting `main`.
Switch to this when the committed HTML becomes noisy in PRs (probably
once the corpus grows past ~50 files).

Today Option A is recommended.

## What the renderer does

- **Parses YAML frontmatter** from every `docs/help/*.md`. Files where
  `audience: public` is missing or `false` are skipped.
- **Renders markdown** with `python-markdown` (extensions: `fenced_code`,
  `tables`, `toc`, `sane_lists`).
- **Rewrites intra-corpus links** — `[Users](users.md)` becomes
  `users.html` so the relative-link pattern keeps working.
- **Strips "Help: " from the first H1** of each topic page so the
  rendered title reads as the concept name.
- **Generates the index** by walking the explicit `SECTIONS` mapping at
  the top of [build_help_site.py](build_help_site.py). Files not listed
  in any section fall into "Other" — that's a signal you should either
  add the file's slug to a section or split a new section.
- **Wraps every page** in a shared shell with the landing-page nav, a
  footer with an "edit this page" GitHub link, and the shared CSS / JS.

## Section mapping lives in code, not frontmatter

The slug-to-section mapping is hardcoded in `SECTIONS` at the top of the
build script. Lifting it to per-file frontmatter (`section: improve`) is
fine if the corpus grows past one section reshuffle per quarter — but for
~20 files, a small explicit list is more readable than 20 individual
frontmatter changes.

When you reshuffle: edit `SECTIONS`, re-run the build, eyeball the index.
Done.

## How this fits the rest of the system

- The same `docs/help/` corpus backs evo's `evolve-knowledge` skill
  ([packages/analyzer/evolve_bot/skills/evolve-knowledge/SKILL.md](../packages/analyzer/evolve_bot/skills/evolve-knowledge/SKILL.md))
  and Atlas's research app's `evolve`-routed retrieval path
  ([docs/atlas-app-manifests/atlas-on-demand-research.json](../docs/atlas-app-manifests/atlas-on-demand-research.json)).
- The weekly knowledge-refresh routine
  ([docs/routines/weekly-knowledge-refresh.md](../docs/routines/weekly-knowledge-refresh.md))
  keeps the corpus current. The site rebuilds on every push to `main`.
- The corpus contract is in
  [docs/help/README.md](../docs/help/README.md). Read it before adding
  new files.

## When to swap in a real SSG

Indicators that the small-build script has outlived its usefulness:

- Corpus exceeds ~50 files and the index becomes unwieldy.
- You need versioned docs (v1.0, v1.1, v2.0).
- You need i18n / multilingual.
- You need search across the full body, not just titles + concepts.

In that case, **MkDocs Material** is the smallest jump (Python, reads the
same frontmatter, has all the above). The frontmatter contract was
deliberately designed to be SSG-portable — no migration of the corpus
itself is needed, only the build pipeline.
