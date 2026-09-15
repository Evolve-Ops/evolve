// Evolve Pods — desktop shell.
//
// ONE macOS window, ONE Dock icon. A thin local "chrome" webview paints the tab
// bar across the top; each configured pod gets its own child webview navigated
// to that pod's adminBaseUrl. Switching a tab repositions webviews — the active
// pod fills the content area, the rest are parked off-screen. Each pod webview
// is an independent origin, so each keeps its own `evolve_device` pairing cookie
// in the shared cookie store. No iframes, no cross-pod bridge, no pod-server
// changes.
//
// SECURITY (auditor-grade): only the local `chrome` webview is granted IPC, via
// capabilities/chrome.json (`"webviews": ["chrome"]`, `"local": true`). The
// remote pod webviews are in NO capability and list NO remote URL anywhere, so
// every IPC invocation from a pod page is denied by the Tauri ACL. A compromised
// pod page therefore cannot reach any Tauri command or native API. See
// capabilities/chrome.json and README.md "Security posture".
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::path::PathBuf;
use std::sync::Mutex;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tauri::menu::{Menu, MenuItemBuilder, SubmenuBuilder};
use tauri::webview::WebviewBuilder;
use tauri::window::WindowBuilder;
use tauri::{
    AppHandle, LogicalPosition, LogicalSize, Manager, Position, Size, State, TitleBarStyle,
    WebviewUrl, WindowEvent,
};

/// Height of the tab strip (logical px). The chrome webview owns this band; pod
/// webviews start just below it. Hidden (0) when ≤1 pod is configured.
const BAR_HEIGHT: f64 = 40.0;

/// First-pod default until the operator adds real pods. Placeholder only — never
/// a real hostname (public-launch scrub invariant).
const DEFAULT_INITIAL_WINDOW: (f64, f64) = (1200.0, 800.0);

/// TCP-connect timeout for the reachability probe behind a pod tab.
const PROBE_TIMEOUT: Duration = Duration::from_millis(2500);

#[derive(Clone, Debug, Serialize, Deserialize)]
struct Pod {
    id: String,
    name: String,
    url: String,
}

/// Shell-local state. Pods persist to `pods.json` in the app config dir; the
/// rest is runtime UI state.
struct Shell {
    config_path: PathBuf,
    /// Sidecar storing the last-active pod id so launch reopens it.
    ui_path: PathBuf,
    pods: Vec<Pod>,
    /// id of the pod currently meant to fill the content area.
    active: Option<String>,
    /// When false, the active pod is parked off-screen so the chrome webview
    /// (settings / empty-state / dead-pod error) shows through.
    show_active: bool,
    /// Pod ids whose webview has been created (lazily, on first show).
    created: Vec<String>,
    /// Pod ids whose webview has successfully loaded its remote URL at least
    /// once since the last failure (drives retry-reload).
    loaded: Vec<String>,
}

impl Shell {
    fn find(&self, id: &str) -> Option<&Pod> {
        self.pods.iter().find(|p| p.id == id)
    }
}

#[derive(Serialize)]
struct ShowResult {
    reachable: bool,
    name: String,
}

fn pod_label(id: &str) -> String {
    format!("pod-{id}")
}

fn lpos(x: f64, y: f64) -> Position {
    Position::Logical(LogicalPosition::new(x, y))
}

fn lsize(w: f64, h: f64) -> Size {
    Size::Logical(LogicalSize::new(w, h))
}

// ---- persistence -------------------------------------------------------------

fn load_pods(path: &PathBuf) -> Vec<Pod> {
    match std::fs::read_to_string(path) {
        Ok(text) => serde_json::from_str(&text).unwrap_or_default(),
        Err(_) => Vec::new(),
    }
}

fn save_pods(path: &PathBuf, pods: &[Pod]) -> Result<(), String> {
    let json = serde_json::to_string_pretty(pods).map_err(|e| e.to_string())?;
    let tmp = path.with_extension("json.tmp");
    std::fs::write(&tmp, json).map_err(|e| e.to_string())?;
    std::fs::rename(&tmp, path).map_err(|e| e.to_string())
}

fn save_last_active(path: &PathBuf, id: &str) {
    let _ = std::fs::write(path, serde_json::json!({ "last_active": id }).to_string());
}

fn load_last_active(path: &PathBuf) -> Option<String> {
    let text = std::fs::read_to_string(path).ok()?;
    let v: serde_json::Value = serde_json::from_str(&text).ok()?;
    v.get("last_active")?.as_str().map(str::to_string)
}

// ---- reachability ------------------------------------------------------------

/// True if the pod's Evolve admin server answers a healthy `/api/health`.
///
/// Deliberately an HTTP health check, NOT a bare TCP connect to host:port. A
/// remote pod's admin binds loopback and is fronted by `tailscale serve` on :443
/// (a local pod is often reached through an SSH port-forward); the fronting
/// listener keeps accepting TCP even while the admin behind it is mid-restart, so
/// a bare-TCP probe reads "reachable" for the entire window the admin is actually
/// down — exactly when a pod webview goes blank and needs reloading. `/api/health`
/// is unauthenticated, so a pod sitting on its `/pair` gate still reports
/// reachable; only a genuinely down/restarting admin reads as unreachable. That
/// accuracy is what drives the dead-pod overlay, the reload-on-recovery path, and
/// the chrome's restart-aware auto-reload.
fn probe(url: &str) -> bool {
    http_health_ok(url, PROBE_TIMEOUT)
}

// ---- discovery ---------------------------------------------------------------
//
// Find Evolve pods the operator can one-click add, without typing a URL:
//   * loopback — the pod on this machine, or one tunneled to localhost
//   * tailscale — peers on the operator's tailnet whose admin is exposed
//     (typically via `tailscale serve` on https/443, since the admin binds
//      loopback and isn't on :5050 over the tailnet)
// Discovery only saves typing — each discovered pod still shows its own /pair
// gate on first open. These commands are reachable only from the local chrome
// webview (capability scoping), never from pod content.

#[derive(Serialize, Clone)]
struct Candidate {
    name: String,
    url: String,
    source: String, // "local" | "tailscale"
}

/// Result of one tailnet scan. The Failed arm carries an operator-facing reason
/// so the UI can say *why* nothing surfaced instead of a bare "nothing found" —
/// a silently-empty discovery is the worst outcome, because the operator can't
/// tell whether the scan ran at all.
#[derive(Debug, PartialEq)]
enum TailscaleScan {
    /// Scan ran; here are the online peers' (name, dns) to health-probe. An
    /// empty vec is a legitimate "ran, no online peers".
    Targets(Vec<(String, String)>),
    /// Scan could not run (no CLI, exec error, not connected, unparseable).
    Failed(String),
}

/// What `discover_pods` hands the chrome webview: the addable candidates plus an
/// optional explanation when the tailnet scan itself could not run. `local`
/// discovery never populates `tailscale_error`; only the tailnet leg does.
#[derive(Serialize, Clone, Default)]
struct Discovery {
    candidates: Vec<Candidate>,
    tailscale_error: Option<String>,
}

/// True iff the body looks like an Evolve admin `/api/health` response.
fn is_evolve_health(body: &str) -> bool {
    serde_json::from_str::<serde_json::Value>(body).map_or(false, |v| {
        v.get("status").and_then(|s| s.as_str()) == Some("ok")
            && v.get("uptime_seconds").map_or(false, |u| u.is_number())
            && v.get("version").is_some()
    })
}

/// Extract `uptime_seconds` from an Evolve `/api/health` body, or `None` if the
/// body isn't a valid Evolve health response. Pure (no I/O) so the down-vs-up
/// boundary and the restart-detection signal (a later uptime < an earlier one)
/// are unit-testable.
fn parse_health_uptime(body: &str) -> Option<u64> {
    if !is_evolve_health(body) {
        return None;
    }
    // The signature already proved `uptime_seconds` is a number; default to 0
    // only as defence against a non-integer value.
    Some(
        serde_json::from_str::<serde_json::Value>(body)
            .ok()
            .and_then(|v| v.get("uptime_seconds").and_then(serde_json::Value::as_u64))
            .unwrap_or(0),
    )
}

/// GET `<base>/api/health`. Returns `Some(uptime_seconds)` when the admin server
/// is actually up (valid Evolve health body), else `None`. `/api/health` is
/// unauthenticated, so `Some` means "admin reachable" independent of pairing.
/// The uptime lets callers notice a restart that happened under a pod webview (a
/// drop between polls) and reload its now-stale content. Read-only.
fn health(base: &str, timeout: Duration) -> Option<u64> {
    let url = format!("{}/api/health", base.trim_end_matches('/'));
    let agent = ureq::AgentBuilder::new().timeout(timeout).build();
    let body = agent.get(&url).call().ok()?.into_string().ok()?;
    parse_health_uptime(&body)
}

/// Convenience bool wrapper over [`health`] for callers that don't need uptime
/// (discovery, the reachability `probe`). Read-only.
fn http_health_ok(base: &str, timeout: Duration) -> bool {
    health(base, timeout).is_some()
}

fn local_hostname() -> String {
    std::process::Command::new("/bin/hostname")
        .output()
        .ok()
        .and_then(|o| String::from_utf8(o.stdout).ok())
        .map(|s| s.trim().trim_end_matches(".local").to_string())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "This machine".to_string())
}

fn discover_local() -> Option<Candidate> {
    const URL: &str = "http://localhost:5050";
    http_health_ok(URL, Duration::from_millis(1500)).then(|| Candidate {
        name: local_hostname(),
        url: URL.to_string(),
        source: "local".to_string(),
    })
}

fn tailscale_bin() -> Option<&'static str> {
    const CANDIDATES: &[&str] = &[
        "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
        "/usr/local/bin/tailscale",
        "/opt/homebrew/bin/tailscale",
        "/usr/bin/tailscale",
    ];
    CANDIDATES
        .iter()
        .copied()
        .find(|p| std::path::Path::new(p).exists())
}

/// Probe one tailnet peer at the URL shapes a pod's admin can live behind:
/// `tailscale serve` on 443, then explicit :5050 over https/http.
fn probe_peer(name: &str, dns: &str) -> Option<Candidate> {
    let bases = [
        format!("https://{dns}"),
        format!("https://{dns}:5050"),
        format!("http://{dns}:5050"),
    ];
    bases.into_iter().find_map(|base| {
        http_health_ok(&base, Duration::from_millis(2000)).then(|| Candidate {
            name: name.to_string(),
            url: base,
            source: "tailscale".to_string(),
        })
    })
}

/// Boilerplate operator nudge appended to every scan-failure reason: the
/// tailscale-serve URL shape is what a remote/VPS pod answers on (loopback:5050
/// exposed only via `tailscale serve` on 443), so this is the address to type.
const MANUAL_ADD_HINT: &str =
    "Add the pod manually below with its https://<host>.ts.net address.";

/// First non-empty line of captured stderr, trimmed and length-capped — keeps
/// the operator-facing reason a single tidy sentence.
fn first_line(s: &str) -> String {
    s.lines()
        .map(str::trim)
        .find(|l| !l.is_empty())
        .unwrap_or("")
        .chars()
        .take(160)
        .collect()
}

/// One online peer → its (name, dns) probe target, or None if it's offline,
/// has no usable DNSName, etc. `Self` never appears in the `Peer` map, so the
/// local pod (the loopback case) is naturally excluded.
fn peer_target(p: &serde_json::Value) -> Option<(String, String)> {
    if !p.get("Online").and_then(|b| b.as_bool()).unwrap_or(false) {
        return None;
    }
    let dns = p
        .get("DNSName")
        .and_then(|s| s.as_str())?
        .trim_end_matches('.')
        .to_string();
    if dns.is_empty() {
        return None;
    }
    let name = p
        .get("HostName")
        .and_then(|s| s.as_str())
        .filter(|s| !s.is_empty())
        .unwrap_or(&dns)
        .to_string();
    Some((name, dns))
}

/// Parse `tailscale status --json` stdout into online-peer probe targets. Pure
/// (no I/O) so the JSON-shape handling is unit-testable. A missing `Peer` map is
/// a valid "ran, zero peers", not a failure.
fn parse_tailscale_targets(stdout: &[u8]) -> Result<Vec<(String, String)>, String> {
    let json: serde_json::Value = serde_json::from_slice(stdout)
        .map_err(|e| format!("Couldn't read Tailscale status ({e})."))?;
    let Some(peers) = json.get("Peer").and_then(|p| p.as_object()) else {
        return Ok(Vec::new());
    };
    Ok(peers.values().filter_map(peer_target).collect())
}

/// Classify a finished `tailscale status --json` invocation. Pure (no I/O) so
/// the failure-mode branching — the exact logic the silent-failure fix exists to
/// protect — is unit-testable.
fn classify_scan(success: bool, stdout: &[u8], stderr: &[u8]) -> TailscaleScan {
    if !success {
        // Non-zero exit is typically "stopped / logged out": valid JSON may
        // still be on stdout, but there are no online peers to probe. Surface
        // the CLI's own stderr line when it has one.
        let detail = first_line(&String::from_utf8_lossy(stderr));
        return TailscaleScan::Failed(if detail.is_empty() {
            format!("Tailscale isn't connected. {MANUAL_ADD_HINT}")
        } else {
            format!("Tailscale: {detail} {MANUAL_ADD_HINT}")
        });
    }
    match parse_tailscale_targets(stdout) {
        Ok(targets) => TailscaleScan::Targets(targets),
        Err(e) => TailscaleScan::Failed(format!("{e} {MANUAL_ADD_HINT}")),
    }
}

/// Run `tailscale status --json` and classify the outcome. Every failure path
/// returns a `Failed(reason)` the UI can show — nothing is swallowed into an
/// empty result.
fn scan_tailscale() -> TailscaleScan {
    let Some(bin) = tailscale_bin() else {
        return TailscaleScan::Failed(format!(
            "Tailscale isn't installed (its CLI wasn't found). {MANUAL_ADD_HINT}"
        ));
    };
    match std::process::Command::new(bin)
        .args(["status", "--json"])
        .output()
    {
        Ok(o) => classify_scan(o.status.success(), &o.stdout, &o.stderr),
        Err(e) => TailscaleScan::Failed(format!(
            "Couldn't run the Tailscale CLI ({e}). {MANUAL_ADD_HINT}"
        )),
    }
}

/// Health-probe the given peers concurrently — each peer's URL-shape chain is
/// short-circuited at the first match.
fn probe_targets(targets: &[(String, String)]) -> Vec<Candidate> {
    std::thread::scope(|scope| {
        let handles: Vec<_> = targets
            .iter()
            .map(|(name, dns)| scope.spawn(move || probe_peer(name, dns)))
            .collect();
        handles
            .into_iter()
            .filter_map(|h| h.join().ok().flatten())
            .collect()
    })
}

fn norm_url(url: &str) -> String {
    url.trim_end_matches('/').to_lowercase()
}

fn validate_url(raw: &str) -> Result<String, String> {
    let parsed = tauri::Url::parse(raw).map_err(|_| "Not a valid URL".to_string())?;
    if parsed.scheme() != "http" && parsed.scheme() != "https" {
        return Err("URL must start with http:// or https://".to_string());
    }
    if parsed.host_str().is_none() {
        return Err("URL must include a host".to_string());
    }
    // Normalise: drop any trailing slash for a stable origin string.
    Ok(parsed.as_str().trim_end_matches('/').to_string())
}

// ---- layout ------------------------------------------------------------------

/// Reposition the chrome webview (full window) and every pod webview. The active
/// pod fills the content area below the tab bar; all others park off-screen.
fn relayout(app: &AppHandle, shell: &Shell) {
    let Some(window) = app.get_window("main") else {
        return;
    };
    let scale = window.scale_factor().unwrap_or(1.0);
    let (w, h) = match window.inner_size() {
        Ok(s) => (s.width as f64 / scale, s.height as f64 / scale),
        Err(_) => DEFAULT_INITIAL_WINDOW,
    };
    let bar = if shell.pods.len() > 1 { BAR_HEIGHT } else { 0.0 };

    if let Some(chrome) = app.get_webview("chrome") {
        let _ = chrome.set_position(lpos(0.0, 0.0));
        let _ = chrome.set_size(lsize(w, h));
    }

    for pod in &shell.pods {
        let Some(wv) = app.get_webview(&pod_label(&pod.id)) else {
            continue;
        };
        let is_active = shell.active.as_deref() == Some(pod.id.as_str()) && shell.show_active;
        if is_active {
            let _ = wv.set_position(lpos(0.0, bar));
            let _ = wv.set_size(lsize(w, (h - bar).max(1.0)));
        } else {
            // Park below the visible area; keep size so it stays "warm".
            let _ = wv.set_position(lpos(0.0, h + 1000.0));
        }
    }
}

// ---- commands (callable ONLY by the local `chrome` webview) ------------------

#[tauri::command]
fn list_pods(state: State<'_, Mutex<Shell>>) -> Vec<Pod> {
    state.lock().unwrap().pods.clone()
}

#[tauri::command]
fn add_pod(
    state: State<'_, Mutex<Shell>>,
    app: AppHandle,
    name: String,
    url: String,
) -> Result<Vec<Pod>, String> {
    let clean_url = validate_url(&url)?;
    let name = name.trim().to_string();
    let mut shell = state.lock().unwrap();
    let name = if name.is_empty() {
        tauri::Url::parse(&clean_url)
            .ok()
            .and_then(|u| u.host_str().map(str::to_string))
            .unwrap_or_else(|| "pod".to_string())
    } else {
        name
    };
    // Stable, collision-free id.
    let mut id = format!(
        "{:x}",
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_millis())
            .unwrap_or(0)
    );
    while shell.pods.iter().any(|p| p.id == id) {
        id.push('x');
    }
    shell.pods.push(Pod { id, name, url: clean_url });
    save_pods(&shell.config_path, &shell.pods)?;
    let pods = shell.pods.clone();
    relayout(&app, &shell); // bar may have just appeared (1 → 2 pods)
    Ok(pods)
}

#[tauri::command]
fn remove_pod(
    state: State<'_, Mutex<Shell>>,
    app: AppHandle,
    id: String,
) -> Result<Vec<Pod>, String> {
    let mut shell = state.lock().unwrap();
    shell.pods.retain(|p| p.id != id);
    shell.created.retain(|c| c != &id);
    shell.loaded.retain(|c| c != &id);
    if shell.active.as_deref() == Some(id.as_str()) {
        shell.active = None;
    }
    save_pods(&shell.config_path, &shell.pods)?;
    let pods = shell.pods.clone();
    // Tear down the pod's webview if it exists.
    if let Some(wv) = app.get_webview(&pod_label(&id)) {
        let _ = wv.close();
    }
    relayout(&app, &shell); // bar may have just disappeared (2 → 1 pods)
    Ok(pods)
}

/// Park every pod webview off-screen so the chrome webview is fully visible
/// (used for the settings panel and the empty state).
#[tauri::command]
fn focus_chrome(state: State<'_, Mutex<Shell>>, app: AppHandle) {
    let mut shell = state.lock().unwrap();
    shell.show_active = false;
    relayout(&app, &shell);
}

/// Make `id` the active pod: create its webview lazily, probe reachability, and
/// either fill the content area (reachable) or leave it parked so the chrome
/// renders its inline dead-pod error (unreachable).
#[tauri::command]
async fn show_pod(
    state: State<'_, Mutex<Shell>>,
    app: AppHandle,
    id: String,
) -> Result<ShowResult, String> {
    // Snapshot what we need without holding the lock across the await/probe.
    let (url, name, must_create) = {
        let shell = state.lock().unwrap();
        let pod = shell.find(&id).ok_or("unknown pod")?;
        (pod.url.clone(), pod.name.clone(), !shell.created.contains(&id))
    };

    if must_create {
        let window = app.get_window("main").ok_or("no main window")?;
        let parsed = tauri::Url::parse(&url).map_err(|e| e.to_string())?;
        // Off-screen initial placement; relayout() positions it once active.
        window
            .add_child(
                WebviewBuilder::new(pod_label(&id), WebviewUrl::External(parsed)),
                lpos(0.0, 100_000.0),
                lsize(800.0, 600.0),
            )
            .map_err(|e| e.to_string())?;
        state.lock().unwrap().created.push(id.clone());
    }

    let probe_url = url.clone();
    let reachable = tauri::async_runtime::spawn_blocking(move || probe(&probe_url))
        .await
        .unwrap_or(false);

    {
        let mut shell = state.lock().unwrap();
        shell.active = Some(id.clone());
        shell.show_active = reachable;
        save_last_active(&shell.ui_path, &id);
        if reachable {
            // A freshly-created webview is already loading `url` from its
            // builder — don't re-navigate. Only an EXISTING webview that was
            // last seen dead needs an explicit reload to recover.
            if !must_create && !shell.loaded.contains(&id) {
                if let Some(wv) = app.get_webview(&pod_label(&id)) {
                    if let Ok(parsed) = tauri::Url::parse(&url) {
                        let _ = wv.navigate(parsed);
                    }
                }
            }
            if !shell.loaded.contains(&id) {
                shell.loaded.push(id.clone());
            }
        } else {
            shell.loaded.retain(|c| c != &id); // force reload on next retry
        }
        relayout(&app, &shell);
    }

    Ok(ShowResult { reachable, name })
}

/// Discover Evolve pods (loopback + tailnet) the operator hasn't added yet.
/// Never errors hard — but a tailnet scan that *couldn't run* reports its reason
/// in `tailscale_error` so the UI never mistakes "couldn't scan" for "nothing on
/// the network".
#[tauri::command]
async fn discover_pods(state: State<'_, Mutex<Shell>>) -> Result<Discovery, String> {
    let existing: Vec<String> = {
        let shell = state.lock().unwrap();
        shell.pods.iter().map(|p| norm_url(&p.url)).collect()
    };
    let (found, tailscale_error) = tauri::async_runtime::spawn_blocking(move || {
        let mut out: Vec<Candidate> = Vec::new();
        if let Some(c) = discover_local() {
            out.push(c);
        }
        let tailscale_error = match scan_tailscale() {
            TailscaleScan::Targets(targets) => {
                out.extend(probe_targets(&targets));
                None
            }
            TailscaleScan::Failed(reason) => Some(reason),
        };
        (out, tailscale_error)
    })
    .await
    .map_err(|e| e.to_string())?;

    // Drop pods already configured + any intra-result dupes.
    let mut seen: std::collections::HashSet<String> = existing.into_iter().collect();
    let candidates = found
        .into_iter()
        .filter(|c| seen.insert(norm_url(&c.url)))
        .collect();
    Ok(Discovery {
        candidates,
        tailscale_error,
    })
}

/// Re-probe + reload a pod after a transient failure (the Retry affordance).
#[tauri::command]
async fn retry_pod(
    state: State<'_, Mutex<Shell>>,
    app: AppHandle,
    id: String,
) -> Result<ShowResult, String> {
    show_pod(state, app, id).await
}

/// Force-reload a pod's webview against its URL, regardless of the cached
/// `loaded` state. Two callers: the native "Reload Pod" (⌘R) hatch, and the
/// chrome's restart-aware auto-reload (when a pod's admin uptime drops, its
/// webview content is from the previous process and must be reloaded). No-ops if
/// the pod is unknown or its webview hasn't been created yet (never shown).
#[tauri::command]
fn reload_pod(state: State<'_, Mutex<Shell>>, app: AppHandle, id: String) {
    let url = {
        let shell = state.lock().unwrap();
        match shell.find(&id) {
            Some(pod) => pod.url.clone(),
            None => return,
        }
    };
    if let (Some(wv), Ok(parsed)) = (app.get_webview(&pod_label(&id)), tauri::Url::parse(&url)) {
        let _ = wv.navigate(parsed);
    }
}

/// Rename a pod (settings inline edit). Layout-neutral; tabs re-render in chrome.
#[tauri::command]
fn rename_pod(
    state: State<'_, Mutex<Shell>>,
    id: String,
    name: String,
) -> Result<Vec<Pod>, String> {
    let name = name.trim().to_string();
    if name.is_empty() {
        return Err("Name can't be empty".to_string());
    }
    let mut shell = state.lock().unwrap();
    match shell.pods.iter_mut().find(|p| p.id == id) {
        Some(pod) => pod.name = name,
        None => return Err("unknown pod".to_string()),
    }
    save_pods(&shell.config_path, &shell.pods)?;
    Ok(shell.pods.clone())
}

/// Reorder a pod one slot up or down (settings). Changes tab order.
#[tauri::command]
fn move_pod(
    state: State<'_, Mutex<Shell>>,
    app: AppHandle,
    id: String,
    up: bool,
) -> Result<Vec<Pod>, String> {
    let mut shell = state.lock().unwrap();
    let idx = shell.pods.iter().position(|p| p.id == id).ok_or("unknown pod")?;
    let swap_with = if up {
        idx.checked_sub(1)
    } else if idx + 1 < shell.pods.len() {
        Some(idx + 1)
    } else {
        None
    };
    if let Some(j) = swap_with {
        shell.pods.swap(idx, j);
        save_pods(&shell.config_path, &shell.pods)?;
    }
    let pods = shell.pods.clone();
    relayout(&app, &shell);
    Ok(pods)
}

#[derive(Serialize)]
struct Reach {
    id: String,
    reachable: bool,
    /// Admin uptime (seconds) when reachable, else `None`. A drop between polls
    /// means the admin restarted under the pod webview, so the chrome reloads it.
    uptime: Option<u64>,
}

/// Liveness of every configured pod (drives the per-tab dots + the chrome's
/// restart-aware auto-reload). Parallel `/api/health` probes; never errors hard.
#[tauri::command]
async fn pods_reachability(state: State<'_, Mutex<Shell>>) -> Result<Vec<Reach>, String> {
    let pods: Vec<(String, String)> = {
        let shell = state.lock().unwrap();
        shell.pods.iter().map(|p| (p.id.clone(), p.url.clone())).collect()
    };
    let out = tauri::async_runtime::spawn_blocking(move || {
        std::thread::scope(|scope| {
            let handles: Vec<_> = pods
                .iter()
                .map(|(id, url)| {
                    scope.spawn(move || {
                        let uptime = health(url, PROBE_TIMEOUT);
                        Reach {
                            id: id.clone(),
                            reachable: uptime.is_some(),
                            uptime,
                        }
                    })
                })
                .collect();
            handles.into_iter().filter_map(|h| h.join().ok()).collect::<Vec<_>>()
        })
    })
    .await
    .map_err(|e| e.to_string())?;
    Ok(out)
}

/// The pod to reopen on launch (last viewed), if still configured.
#[tauri::command]
fn last_active(state: State<'_, Mutex<Shell>>) -> Option<String> {
    load_last_active(&state.lock().unwrap().ui_path)
}

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            list_pods,
            add_pod,
            remove_pod,
            focus_chrome,
            show_pod,
            retry_pod,
            reload_pod,
            discover_pods,
            rename_pod,
            move_pod,
            pods_reachability,
            last_active
        ])
        // The macOS menu bar is the always-available management hatch. With ≤1
        // pod the tab strip is hidden and the pod webview fills the window, so
        // "Pods → Manage Pods…" is how the operator reaches settings. It parks
        // the pods and asks the chrome to open its settings panel (eval into the
        // LOCAL chrome webview only — never a pod webview).
        .on_menu_event(|app, event| {
            let id = event.id().as_ref();
            if id == "manage_pods" {
                if let Some(state) = app.try_state::<Mutex<Shell>>() {
                    let mut shell = state.lock().unwrap();
                    shell.show_active = false;
                    relayout(app, &shell);
                }
                if let Some(chrome) = app.get_webview("chrome") {
                    let _ = chrome.eval("window.openSettings && window.openSettings()");
                }
                return;
            }
            if id == "reload_pod" {
                // Force-reload the active pod's webview — the manual escape hatch
                // for a stale/blank page. Driven through the chrome so it reuses
                // the same activeId + reload_pod command the auto-reload uses.
                if let Some(chrome) = app.get_webview("chrome") {
                    let _ = chrome.eval("window.reloadActive && window.reloadActive()");
                }
                return;
            }
            // Keyboard tab switching → drive the chrome (works regardless of
            // which webview has focus, since menu accelerators fire globally).
            let js = match id {
                "next_pod" => Some("window.cycleTab && window.cycleTab(1)".to_string()),
                "prev_pod" => Some("window.cycleTab && window.cycleTab(-1)".to_string()),
                s if s.starts_with("pod_") => s[4..]
                    .parse::<u32>()
                    .ok()
                    .map(|n| format!("window.gotoTab && window.gotoTab({n})")),
                _ => None,
            };
            if let Some(js) = js {
                if let Some(chrome) = app.get_webview("chrome") {
                    let _ = chrome.eval(&js);
                }
            }
        })
        .setup(|app| {
            let config_dir = app.path().app_config_dir()?;
            std::fs::create_dir_all(&config_dir).ok();
            let config_path = config_dir.join("pods.json");
            let ui_path = config_dir.join("ui_state.json");
            let pods = load_pods(&config_path);

            app.manage(Mutex::new(Shell {
                config_path,
                ui_path,
                pods,
                active: None,
                show_active: true,
                created: Vec::new(),
                loaded: Vec::new(),
            }));

            let (w0, h0) = DEFAULT_INITIAL_WINDOW;
            // Overlay title bar: the webview content fills the whole window
            // frame (traffic lights float over the top-left), so child-webview
            // coordinates measure from the true top-left. With the default
            // visible title bar, child positions are offset by the title-bar
            // height and the active pod creeps up over the tab strip.
            let window = WindowBuilder::new(app, "main")
                .title("Evolve Pods")
                .inner_size(w0, h0)
                .min_inner_size(640.0, 480.0)
                .title_bar_style(TitleBarStyle::Overlay)
                .hidden_title(true)
                .build()?;

            // The local chrome (tab bar + settings/empty/error surfaces) fills
            // the window and sits beneath every pod webview.
            window.add_child(
                WebviewBuilder::new("chrome", WebviewUrl::App("index.html".into())),
                lpos(0.0, 0.0),
                lsize(w0, h0),
            )?;

            // Native menu: standard app menu + a "Pods" submenu with the
            // always-available "Manage Pods…" hatch and keyboard tab switching.
            let manage = MenuItemBuilder::with_id("manage_pods", "Manage Pods…")
                .accelerator("CmdOrCtrl+,")
                .build(app)?;
            // Manual escape hatch: force-reload the active pod's webview when its
            // content is stale/blank (e.g. a navigation that failed during an
            // admin restart). The auto-reload handles the common case; this is the
            // always-available fallback.
            let reload = MenuItemBuilder::with_id("reload_pod", "Reload Pod")
                .accelerator("CmdOrCtrl+R")
                .build(app)?;
            let prev = MenuItemBuilder::with_id("prev_pod", "Previous Pod")
                .accelerator("Ctrl+Shift+Tab")
                .build(app)?;
            let next = MenuItemBuilder::with_id("next_pod", "Next Pod")
                .accelerator("Ctrl+Tab")
                .build(app)?;
            // Cmd+1..9 jump to a pod by position.
            let goto_items: Vec<_> = (1u32..=9)
                .map(|n| {
                    MenuItemBuilder::with_id(format!("pod_{n}"), format!("Go to Pod {n}"))
                        .accelerator(format!("CmdOrCtrl+{n}"))
                        .build(app)
                })
                .collect::<Result<_, _>>()?;
            let mut builder = SubmenuBuilder::new(app, "Pods")
                .item(&manage)
                .item(&reload)
                .separator()
                .item(&prev)
                .item(&next)
                .separator();
            for it in &goto_items {
                builder = builder.item(it);
            }
            let pods_submenu = builder.build()?;
            let menu = Menu::default(app.handle())?;
            menu.append(&pods_submenu)?;
            app.set_menu(menu)?;

            // Keep layout correct on resize.
            let handle = app.handle().clone();
            window.on_window_event(move |event| {
                if matches!(event, WindowEvent::Resized(_)) {
                    if let Some(state) = handle.try_state::<Mutex<Shell>>() {
                        let shell = state.lock().unwrap();
                        relayout(&handle, &shell);
                    }
                }
            });

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running the Evolve Pods shell");
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    // Placeholder tailnet + hostnames only — never a real address (public-launch
    // scrub invariant). `examplenet.ts.net` is non-routable illustration.

    #[test]
    fn health_signature_matches_evolve() {
        assert!(is_evolve_health(
            r#"{"status":"ok","uptime_seconds":42,"version":"0.1.0"}"#
        ));
    }

    #[test]
    fn health_uptime_parses_when_evolve() {
        assert_eq!(
            parse_health_uptime(r#"{"status":"ok","uptime_seconds":403,"version":"0.1.0"}"#),
            Some(403)
        );
    }

    #[test]
    fn health_uptime_none_when_not_evolve() {
        // A /pair gate (HTML), a non-ok status, or any non-health page all mean
        // "admin not confirmed up" — None, so the probe reads unreachable. This is
        // the whole point of the health-based probe: tailscaled/SSH-forward still
        // answers TCP, but these bodies prove the admin behind it is down.
        assert_eq!(parse_health_uptime("<!doctype html><title>Pair</title>"), None);
        assert_eq!(
            parse_health_uptime(r#"{"status":"degraded","uptime_seconds":5,"version":"0.1.0"}"#),
            None
        );
    }

    #[test]
    fn health_uptime_drop_signals_restart() {
        // The auto-reload trigger: a later uptime lower than an earlier one means
        // the admin restarted under the webview (its content is now stale).
        let before =
            parse_health_uptime(r#"{"status":"ok","uptime_seconds":600,"version":"0.1.0"}"#).unwrap();
        let after =
            parse_health_uptime(r#"{"status":"ok","uptime_seconds":4,"version":"0.1.0"}"#).unwrap();
        assert!(after < before, "uptime reset after restart must read as a drop");
    }

    #[test]
    fn health_signature_rejects_non_evolve() {
        // Missing version.
        assert!(!is_evolve_health(r#"{"status":"ok","uptime_seconds":42}"#));
        // Wrong status.
        assert!(!is_evolve_health(
            r#"{"status":"degraded","uptime_seconds":42,"version":"0.1.0"}"#
        ));
        // Non-numeric uptime.
        assert!(!is_evolve_health(
            r#"{"status":"ok","uptime_seconds":"up","version":"0.1.0"}"#
        ));
        // Not even JSON.
        assert!(!is_evolve_health("<html>404</html>"));
    }

    fn status_with(peers: serde_json::Value) -> Vec<u8> {
        json!({ "BackendState": "Running", "Peer": peers })
            .to_string()
            .into_bytes()
    }

    #[test]
    fn online_peer_becomes_target() {
        let out = status_with(json!({
            "nodekey:aaa": {
                "Online": true,
                "HostName": "pod-alpha",
                "DNSName": "pod-alpha.examplenet.ts.net."
            }
        }));
        let targets = parse_tailscale_targets(&out).unwrap();
        assert_eq!(
            targets,
            vec![(
                "pod-alpha".to_string(),
                "pod-alpha.examplenet.ts.net".to_string() // trailing dot trimmed
            )]
        );
    }

    #[test]
    fn offline_and_unusable_peers_are_skipped() {
        let out = status_with(json!({
            "nodekey:off": {
                "Online": false,
                "HostName": "pod-offline",
                "DNSName": "pod-offline.examplenet.ts.net."
            },
            "nodekey:nodns": {
                "Online": true,
                "HostName": "pod-nodns"
            },
            "nodekey:empty": {
                "Online": true,
                "HostName": "pod-empty",
                "DNSName": "."
            }
        }));
        assert!(parse_tailscale_targets(&out).unwrap().is_empty());
    }

    #[test]
    fn missing_hostname_falls_back_to_dns() {
        let out = status_with(json!({
            "nodekey:bbb": {
                "Online": true,
                "DNSName": "pod-bravo.examplenet.ts.net."
            }
        }));
        let targets = parse_tailscale_targets(&out).unwrap();
        assert_eq!(targets.len(), 1);
        let (name, dns) = &targets[0];
        assert_eq!(name, "pod-bravo.examplenet.ts.net");
        assert_eq!(dns, "pod-bravo.examplenet.ts.net");
    }

    #[test]
    fn no_peer_map_is_empty_not_error() {
        let out = json!({ "BackendState": "Running" }).to_string().into_bytes();
        assert_eq!(parse_tailscale_targets(&out).unwrap(), Vec::new());
    }

    #[test]
    fn unparseable_status_is_error() {
        assert!(parse_tailscale_targets(b"not json at all").is_err());
    }

    #[test]
    fn classify_success_with_online_peer_yields_targets() {
        let stdout = status_with(json!({
            "nodekey:ccc": {
                "Online": true,
                "HostName": "pod-charlie",
                "DNSName": "pod-charlie.examplenet.ts.net."
            }
        }));
        match classify_scan(true, &stdout, b"") {
            TailscaleScan::Targets(t) => assert_eq!(t.len(), 1),
            other => panic!("expected Targets, got {other:?}"),
        }
    }

    #[test]
    fn classify_success_with_no_peers_is_empty_targets_not_failure() {
        // The bug-prone case: scan ran fine, network just has no online peers.
        // Must be Targets([]) — NOT Failed — so the UI says "nothing new found",
        // not "couldn't scan".
        let stdout = json!({ "BackendState": "Running" }).to_string().into_bytes();
        assert_eq!(classify_scan(true, &stdout, b""), TailscaleScan::Targets(vec![]));
    }

    #[test]
    fn classify_nonzero_exit_surfaces_stderr_reason() {
        let scan = classify_scan(false, b"", b"needs login; run 'tailscale up'\n");
        match scan {
            TailscaleScan::Failed(msg) => {
                assert!(msg.contains("needs login"), "got: {msg}");
                assert!(msg.contains("ts.net"), "should include manual-add hint: {msg}");
            }
            other => panic!("expected Failed, got {other:?}"),
        }
    }

    #[test]
    fn classify_nonzero_exit_without_stderr_falls_back() {
        match classify_scan(false, b"", b"") {
            TailscaleScan::Failed(msg) => assert!(msg.contains("isn't connected"), "got: {msg}"),
            other => panic!("expected Failed, got {other:?}"),
        }
    }

    #[test]
    fn classify_success_but_unparseable_is_failed() {
        match classify_scan(true, b"garbage", b"") {
            TailscaleScan::Failed(msg) => assert!(msg.contains("Couldn't read"), "got: {msg}"),
            other => panic!("expected Failed, got {other:?}"),
        }
    }

    #[test]
    fn first_line_picks_first_nonempty_and_caps() {
        assert_eq!(first_line("\n\n  hello world \n more"), "hello world");
        assert_eq!(first_line(""), "");
        assert_eq!(first_line(&"x".repeat(500)).len(), 160);
    }
}
