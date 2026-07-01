# GitHub Pages destination — decision spec (2026-06-04)

**Status:** Decision needed before cutover. Recommendation: **Option B (apex domain `evolveops.dev`).**

**Date:** 2026-06-04

**Origin:** Pre-cutover setup. The current GitHub Pages site lives under `cjalden.github.io/evolve` (or equivalent) and moves to a new home when the repo transfers to `evolve-ops/evolve`. Settling the destination now lets DNS propagate before the cutover happens.

**Adjacent:**

- `docs/runbook-public-cutover.md` Phase 7 — GitHub Pages activation
- `docs/gitpages/` — the current site source (Jekyll, served from the repo)

---

## Options

### Option A — Project pages at `evolve-ops.github.io/evolve`

GitHub Pages' default. URL is auto-derived from the org + repo names.

- **Pros**: zero DNS setup; works immediately on transfer; no certificate provisioning to wait on; no recurring DNS bill.
- **Cons**: URL is less brandable (`evolve-ops.github.io/evolve` has a GitHub-y stem); the `/evolve` subpath gets baked into every external reference; doesn't take advantage of the `evolveops.dev` domain you already pay for.

### Option B — Custom domain at apex `evolveops.dev` (recommended)

Site is served at `https://evolveops.dev` directly. GitHub Pages handles HTTPS via Let's Encrypt.

- **Pros**: cleanest URL; reads as the product, not as a GitHub project; matches the contact email (`hello@evolveops.dev`); future blog posts / docs / marketing all link to one stem.
- **Cons**: locks the apex to one project. If adjacent projects spin up later (CLI tool, marketplace, separate microsite), they'd need subdomains. Requires DNS configuration once; nothing recurring.

### Option C — Custom domain at subdomain `evolve.evolveops.dev`

Apex left open for future use; Evolve specifically lives at a subdomain.

- **Pros**: keeps `evolveops.dev` apex free for a marketing/landing page or other property. More flexible long-term.
- **Cons**: longer URL; `evolveops.dev → evolve.evolveops.dev` reads as redundant ("evolve" twice). Also: if Evolve IS the only product, putting it on a subdomain implies a marketing site at the apex that doesn't exist.

### Option D — Hybrid (`evolveops.dev` apex → marketing site; `evolveops.dev/evolve` → Pages)

Apex serves a small marketing landing; the actual product docs live at a path.

- **Pros**: most flexible; clean future story for adjacent projects.
- **Cons**: most setup; requires a separate marketing-site host (probably a small Vercel/Netlify deploy or a static apex page). Overkill for a single-product pre-launch.

---

## Recommendation

**Option B — apex `evolveops.dev`.**

Three reasons:

1. **Evolve is the only product.** Putting it on a subdomain implies a sibling at the apex that doesn't exist and won't exist for a long time.
2. **The brand IS evolveops.dev.** That's what the domain was acquired for. Marketing pieces, SECURITY.md contact, and future investor / partner conversations all use that string. Resolving it to the actual product page is the natural fit.
3. **Future flexibility isn't actually impaired.** Adjacent projects (CLI tool, marketplace, mobile app, etc.) get subdomains like `cli.evolveops.dev`, `marketplace.evolveops.dev`. The apex hosting Evolve doesn't prevent that.

If you ever decide the apex should be a marketing landing with Evolve at `/evolve` or a subdomain, the migration is one DNS change + one PR. Not locked in.

---

## DNS configuration (for Option B)

At your DNS provider for `evolveops.dev`, add these records:

**Apex `evolveops.dev`** (IPv4 + IPv6 A/AAAA records to GitHub Pages):

```
@   A    185.199.108.153
@   A    185.199.109.153
@   A    185.199.110.153
@   A    185.199.111.153
@   AAAA 2606:50c0:8000::153
@   AAAA 2606:50c0:8001::153
@   AAAA 2606:50c0:8002::153
@   AAAA 2606:50c0:8003::153
```

(GitHub's [Pages apex domain IPs](https://docs.github.com/en/pages/configuring-a-custom-domain-for-your-github-pages-site/managing-a-custom-domain-for-your-github-pages-site#configuring-an-apex-domain). They've been stable; check the docs link before cutover in case the list updates.)

**`www.evolveops.dev` redirect** (optional but recommended):

```
www  CNAME  evolve-ops.github.io.
```

GitHub Pages auto-redirects `www.evolveops.dev` → `evolveops.dev` if both records are in place.

---

## Repo-side configuration

In `evolve-ops/evolve` (post-cutover; or in `evolve-ops/evolve` pre-transfer and migrate with the repo):

1. **Add a `CNAME` file** at the Pages source root (`docs/gitpages/CNAME` if that's the Pages source dir) containing exactly:
   ```
   evolveops.dev
   ```
   No protocol, no path. Just the domain.

2. **In GitHub Settings → Pages**: set Source = main / docs/gitpages, Custom domain = `evolveops.dev`, Enforce HTTPS = on (after the cert provisions, usually within 15 minutes).

3. **Verify** by visiting `https://evolveops.dev` post-DNS-propagation. Should serve the gitpages content with a valid certificate.

---

## Pre-cutover steps (what to do now)

1. **Configure DNS now** (records above). Propagation can take minutes to hours; doing it now means it's ready by cutover day.
2. **Add the `CNAME` file** to `docs/gitpages/CNAME` in the current `evolve-ops/evolve` repo. It won't take effect until DNS + Pages-config are both pointing at it, but having it in the repo means it migrates with the transfer.
3. **Test DNS resolution** before cutover: `dig evolveops.dev +short` should return the GitHub Pages IPs once propagated.
4. **Update runbook Phase 7** to reference `evolveops.dev` as the destination instead of leaving it as a TODO.

---

## Post-cutover verification (Runbook Phase 7 addition)

- [ ] `https://evolveops.dev` resolves and serves the gitpages content
- [ ] HTTPS certificate is valid (GitHub-managed Let's Encrypt)
- [ ] `www.evolveops.dev` redirects to apex
- [ ] All internal anchors / relative links work (no broken cross-page references)
- [ ] `dig evolveops.dev` returns GitHub's apex IPs (confirms apex domain config landed)

---

## Out of scope

- **A separate marketing site** (a `evolveops.com`-style sales page). If/when that becomes a priority, it goes on a different stack (Vercel, Webflow, etc.) and the apex DNS gets reassigned.
- **Subdomains for adjacent projects.** When the first adjacent project appears, decide its subdomain then.
- **Email hosting at `evolveops.dev`.** The `hello@evolveops.dev` mailbox is set up separately (you mentioned acquiring it); MX records for that are independent of the Pages A/AAAA records and don't conflict.
