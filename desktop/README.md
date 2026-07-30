# Evolve Pods — desktop shell

A small macOS desktop app that puts **one Dock/Finder icon** over your Evolve
pods and lets you **tab between them**. It is pure navigation: each pod opens in
its own webview pointed at that pod's admin URL, keeps its own login, and is an
independent island. The shell adds **zero new surface on any pod** — no
cross-pod bridge, no aggregation, no shared state, no tokens.

This is milestone **M2.5** of the multi-pod design
(`../docs/design-multi-pod-2026-06-11.md`,
§3 "The one-app question" + §8). It is the native-shell answer to "can it be
one app that toggles among pods, not one install per pod?" — desktop-first,
built in parallel with the Linux port and with no dependency on `deploy.py` or
any pod-server code.

Built with **Tauri v2** (chosen over Electron for the lighter binary).

---

## What it does

- **One window, one icon.** A single macOS `.app`.
- **One webview per pod.** Each configured pod gets its own child webview
  navigated to its `adminBaseUrl` (a remote `http(s)` URL). Not an iframe —
  iframes break cross-origin cookies and hit `X-Frame-Options`. Because the
  webviews share the app's cookie store and **cookies are per-origin**, each
  pod's `evolve_device` pairing cookie coexists, so per-origin pairing keeps
  working unchanged. First visit to an unpaired pod shows that pod's own
  `/pair` gate inside its tab — that is correct, leave it.
- **A thin tab bar** across the top lists pods by name and raises the chosen
  pod's webview. The tab bar is **hidden entirely when ≤1 pod** is configured —
  a single-pod operator just sees that pod full-window. With one pod the
  **"Pods → Manage Pods…"** menu (⌘,) is how you reach settings.
- **Offline tolerant.** The shell always opens, even if a pod is down. A pod
  that fails a reachability probe shows an inline "Can't reach <pod>" error
  with a **Retry** button; other tabs and the shell are unaffected. One dead
  pod never blocks the app.
- **Pod discovery (one-click add).** The settings / empty state offers detected
  pods so you don't type a URL: a **loopback** probe of `localhost:5050` (the pod
  on this machine, or one tunneled to localhost) and a **Tailscale** sweep
  (`tailscale status --json` → probe each online peer at `https://<peer>/api/health`,
  then `:5050` https/http — since the admin binds loopback and is usually exposed
  over the tailnet via `tailscale serve` on 443). A candidate is only shown if it
  returns the Evolve `/api/health` signature (`status:"ok"` + `uptime_seconds` +
  `version`). Discovery only saves typing — each added pod still shows its own
  `/pair` gate. It reads `/api/health` (already auth-exempt) and shells out to the
  local `tailscale` binary from Rust; it adds **no** pod-server surface, and these
  commands are reachable only from the local chrome webview.
- **Shell-local pod list.** Pods live in `pods.json` in the app config dir
  (`~/Library/Application Support/ops.evolve.pods/pods.json`), an array of
  `{ "name", "adminBaseUrl" }`. The list ships **empty** — the operator adds
  pods at runtime (name + URL) via the settings panel. It never depends on any
  pod being up.
- **Niceties.** Per-tab **liveness dots** (green = reachable, red = down, polled
  every 8s); **rename** (inline-edit the name in settings) and **reorder** (↑/↓)
  pods; the shell **reopens the last-viewed pod** on launch (`ui_state.json`);
  keyboard switching via the Pods menu — **Ctrl+Tab / Ctrl+Shift+Tab** to cycle
  and **⌘1–9** to jump; discovery **auto-refreshes** silently while the settings
  panel is open, so a pod that comes up appears without a manual rescan.

---

## Develop & build

Requires **Rust** (`rustup`) and the **Tauri v2 CLI**. Node is *not* required —
the frontend is static HTML/CSS/JS served by Tauri directly.

```sh
# one-time toolchain
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
cargo install tauri-cli --version "^2.0" --locked

# from desktop/src-tauri/
cargo tauri dev      # run in development (hot window)
cargo tauri build    # produce target/release/bundle/macos/Evolve Pods.app (+ .dmg)
```

`cargo tauri build` emits both an `.app` and a `.dmg` (see `bundle.targets` in
`tauri.conf.json`). The app icon is generated from `icons/source-icon.png` via
`cargo tauri icon icons/source-icon.png`; regenerate the source with
`python3 icons/make-source-icon.py` (stdlib-only, no deps).

---

## Security posture (read before changing capabilities)

The shell loads **remote pod pages** as webview content. The one rule that
matters: **remote pod content must not be able to reach any Tauri command or
native API.** It can't, by construction:

1. **Only the local `chrome` webview is granted IPC.**
   [`src-tauri/capabilities/chrome.json`](src-tauri/capabilities/chrome.json)
   is the *only* capability. It scopes `core:default` to `"webviews":
   ["chrome"]` with `"local": true`. No other capability exists.
2. **No remote URL appears in any capability.** In Tauri v2 a webview can only
   invoke commands if a capability authorizes its origin; for remote origins
   that requires listing the URL under `remote.urls`. We list none. So every
   IPC invocation originating from a pod webview (`pod-*` labels) is **denied
   by the Tauri ACL** — a compromised or malicious pod page can call nothing.
3. **`local: true`** further means even the `chrome` webview would lose its
   grant if it were ever navigated to a remote origin. The chrome only ever
   loads the bundled local `index.html`.
4. **`withGlobalTauri` is off** and the app enables **no plugins** (no `fs`,
   `shell`, `http`, …). File reads/writes and the reachability probe happen in
   Rust, not via any frontend-reachable plugin. The native command surface is
   six app commands, all reachable only from `chrome`.

**To verify the boundary:** open a pod page's devtools console and call
`window.__TAURI_INTERNALS__.invoke('list_pods')` (or any command) — it rejects
("… not allowed"). The chrome webview, in the capability, succeeds. Adding a
remote URL to a capability, or adding a `pod-*` label to one, would break the
boundary — don't.

### App Transport Security

`src-tauri/Info.plist` allows plaintext loads (`NSAllowsArbitraryLoads` +
`NSAllowsLocalNetworking`) because pods are reached over the operator's own
trusted infrastructure (`http://localhost`, a LAN hostname, a tailnet address)
and WKWebView blocks plaintext `http://` by default. This governs only the
transport of the operator's own configured pod URLs; it does **not** widen the
IPC/native surface. An all-loopback/`.local` deployment can drop
`NSAllowsArbitraryLoads` and keep only `NSAllowsLocalNetworking`.

---

## What this is NOT

No cross-pod aggregation, no shared state, no fetching one pod's data into
another, no pod-server changes, no CORS, no tokens, no bridge between pods.
Each webview is an independent island. Cross-pod *sharing* (digests, app/config
artifacts) is a separate, later milestone (M3+) and is explicitly out of scope
here.

> **Note on the Rust/CI gate:** Tauri/Rust is a new toolchain for this repo.
> This shell is intentionally **not** wired into the required CI gates yet —
> the existing gates stay green and a Rust build gate is a deferred follow-up.

## Updates & distribution

You do **not** rebuild this app for ordinary Evolve updates — each pod's admin UI
is loaded as live remote content, so a pod that pulls a new release shows its new
UI on next load (like a browser; the shell holds no Evolve code). You only rebuild
when the **shell itself** changes — rare.

The app is currently **unsigned** and has **no auto-updater** — fine for the
operator who builds it (`cargo tauri build` → right-click → Open), not for
handing out. Code-signing + notarization + a self-update feed + a release CI job
are specced and **deferred** in
`../docs/spec-multipod-desktop-distribution-2026-06-19.md`
(milestone M2.6) — build them only when the app leaves the build machine, the
shell-update cadence gets annoying, or Evolve goes public.
