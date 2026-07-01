# Spec: Evolve Pods desktop — signing, auto-update & distribution

**Status:** DEFERRED — spec only, **do not build until a trigger below fires.**
**Date:** 2026-06-19 · **Aspect:** META:multi-pod · **Milestone:** M2.6 (follows
M2.5, [design-multi-pod-2026-06-11.md](design-multi-pod-2026-06-11.md) §8).
**Built artifact this extends:** the M2.5 desktop shell ([../desktop/](../desktop/),
PR #3031).

---

## 1. Why this is small, and why it's deferred

The M2.5 shell loads each pod's admin UI as **live remote web content** — like a
browser pointed at the pod's URL. So **ordinary Evolve updates never require a new
desktop build**: when a pod pulls a new release, its admin UI updates and the
webview shows it on next load. The shell is decoupled from Evolve's release
cadence by construction (this is the same zero-pod-surface property that keeps it
secure).

What this spec covers is the **residual** friction, which is *not* coupled to
Evolve updates:

1. **Shell-self updates.** When the shell's own code changes (tab logic, settings,
   capability config, the ATS plist) the operator currently rebuilds and
   reinstalls by hand. Rare, but manual.
2. **Distribution beyond the build machine.** The M2.5 `.app`/`.dmg` is **unsigned
   and un-notarized**. Fine for the operator who built it (right-click → Open);
   a scary Gatekeeper wall for anyone else, and no way to push fixes out.

**None of this is needed for single-operator personal use.** It only earns its
keep when the app leaves the laptop or shell-update cadence becomes a nuisance.

### Build triggers (build when ANY is true)

- The desktop app is handed to a **second person / machine** you don't build on.
- The shell starts changing **often enough** that manual rebuild-reinstall annoys.
- Evolve **goes public** (evolve-ops/evolve, see [META:public]) and a downloadable
  desktop app becomes part of the offering.

Until a trigger fires: leave it. `cargo tauri build` + drag-to-Applications is the
whole story.

---

## 2. Build plan

Three phases, each independently shippable. Phases 1–2 gate on an **Apple
Developer Program enrollment** ($99/yr) — an operator action, not a code action.

### Phase 1 — Code signing + notarization (~0.5 session after enrollment)

Make the `.app`/`.dmg` open cleanly on any Mac (no Gatekeeper wall).

- **Cert:** a *Developer ID Application* certificate from the Apple Developer
  account (for distribution outside the App Store).
- **Tauri config** (`desktop/src-tauri/tauri.conf.json`): set
  `bundle.macOS.signingIdentity` (the Developer ID), `bundle.macOS.entitlements`
  (a hardened-runtime entitlements plist — notarization requires hardened
  runtime), and `bundle.macOS.providerShortName` if the account has multiple
  teams.
- **Notarization:** `tauri build` notarizes + staples automatically when the
  Apple credentials are present in the environment — either
  `APPLE_ID` + `APPLE_PASSWORD` (an app-specific password) + `APPLE_TEAM_ID`, or
  an App Store Connect API key via `APPLE_API_KEY` / `APPLE_API_ISSUER` /
  `APPLE_API_KEY_PATH`. Prefer the API-key form (revocable, no human Apple ID).
- **Verify:** `spctl -a -vvv "Evolve Pods.app"` reports `accepted / Notarized
  Developer ID`; `stapler validate` passes.

### Phase 2 — Auto-updater (~1 session)

Let the shell update itself so shell-changes stop being a manual chore.

- **Plugin:** `tauri-plugin-updater` (Rust) + `@tauri-apps/plugin-updater` (JS).
- **Update signing keypair** (separate from Apple signing): `tauri signer
  generate` → a minisign keypair. The **public** key is baked into
  `tauri.conf.json` (`plugins.updater.pubkey`); the **private** key signs each
  release's update artifacts at build time
  (`TAURI_SIGNING_PRIVATE_KEY` + `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`).
- **Update feed:** a static JSON manifest (`latest.json`: version, notes,
  pub_date, `platforms."darwin-aarch64".{signature,url}`) the app polls. Host it
  on the **release channel** (Phase 3), **never on a pod** — keeping the shell's
  no-pod-dependency invariant intact.
- **Capability scoping (security-critical):** the updater plugin's permission
  (`updater:default`) is granted to the **local `chrome` webview ONLY**, exactly
  like every other permission in `capabilities/chrome.json`. It must **never** be
  in scope for `pod-*` webviews. A remote pod page must not be able to trigger or
  redirect an update.

### Phase 3 — Release channel + CI build job (~1 session)

Automate signed, notarized, self-updating releases on a version tag. This also
closes the M2.5-deferred "Rust isn't in CI" gap.

- **Channel:** GitHub Releases (natural fit, and aligns with the public-repo plan
  if [META:public] proceeds). The release holds the `.dmg` + the updater
  artifacts + `latest.json`.
- **CI:** a macOS arm64 runner (`macos-14`) job, gated to tags (not every PR),
  using `tauri-apps/tauri-action` to build → sign → notarize → publish the
  release → upload the updater manifest in one pass.
- **Secrets (the custody surface — see §3):** Apple cert (base64 `.p12`) +
  password, Apple API key/issuer, Tauri updater **private** key + password.

### Phase 4 — cross-platform shell (optional, only if needed)

A Linux/Windows build of the shell, only if an operator runs the *shell* itself
on a non-Mac. Tauri supports it; the multiwebview + capability model is
platform-neutral. The macOS-specifics to generalize: the ATS `Info.plist`
(macOS-only; other platforms don't gate plaintext the same way) and the
overlay-title-bar trick (verify child-webview coordinate origin per platform).
Almost certainly out of scope — the operator's admin machine is a Mac.

---

## 3. Security considerations (auditor-grade — route key custody via META:edr / diligence)

The auto-updater introduces the **one genuinely dangerous new asset** in this
whole desktop story: an update channel that can execute new code on the
operator's machine.

- **Updater private key custody.** If the minisign private key is stolen, an
  attacker can sign a malicious update → silent RCE on every machine running the
  shell. It lives **only** as a CI secret, never in the repo, and rotating it
  means shipping a build with a new baked-in pubkey. Treat it like a release
  signing key, because it is one.
- **Feed integrity.** Updates are signature-verified client-side (the pubkey is
  in the app), so a tampered manifest can't push an unsigned payload — but serve
  the feed over HTTPS anyway, and never from a pod.
- **No new pod surface.** The update feed is operator/release infrastructure, not
  a pod. The shell's "zero new pod surface, no cross-pod bridge" invariant is
  unchanged by this spec.
- **Capability discipline carries forward.** Phase 2's updater permission is
  chrome-only; the M2.5 boundary (remote pod content reaches no native command)
  must remain provable after the plugin is added — re-run the M2.5 adversarial
  invoke check against a pod webview as part of Phase 2's acceptance.

---

## 4. Effort & cost

| Phase | Effort | Blocked on |
|---|---|---|
| 1 — signing + notarization | ~0.5 session | Apple Developer enrollment ($99/yr, operator) |
| 2 — auto-updater | ~1 session | updater keypair (generate) + a place for the feed (Phase 3) |
| 3 — release channel + CI | ~1 session | CI secrets provisioned (operator) |
| 4 — cross-platform (optional) | ~1 session | a non-Mac shell host actually existing |

Total once Apple enrollment exists: **~2–3 sessions** for Phases 1–3.

---

## 5. Open questions (resolve when building, not now)

- **Q1.** GitHub Releases vs a static host for the update feed — defaults to
  Releases; revisit if the repo stays private and Releases aren't reachable by
  the app without auth.
- **Q2.** App Store Connect API key vs Apple-ID+app-password for notarization —
  spec prefers the API key (revocable, scriptable). Confirm at build time.
- **Q3.** Update cadence / channel (stable only, or a beta channel) — single
  stable channel for v1.
- **Q4.** Does signing/notarization wait for [META:public]? If the repo and
  identity are about to change, doing Apple enrollment under the final
  org/identity avoids re-doing it. Sequencing note, not a blocker.
