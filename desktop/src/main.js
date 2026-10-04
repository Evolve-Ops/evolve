// Evolve Pods — chrome logic. Drives the tab bar and the settings/empty/error
// surfaces, and asks the Rust side to position the per-pod webviews.
//
// IPC: only this LOCAL webview ("chrome") is granted access by the Tauri ACL
// (capabilities/chrome.json). The remote pod webviews can call nothing.
const invoke = (cmd, args = {}) => window.__TAURI_INTERNALS__.invoke(cmd, args);

let pods = [];
let activeId = null;
let mode = "loading"; // loading | pod | error | settings | empty
let errorPod = null;
let discovered = []; // one-click-add candidates from discover_pods
let discoveryError = null; // why the tailnet scan couldn't run, if it couldn't
let scanning = false;
let reach = {}; // pod id -> bool reachability (drives the per-tab dots)
let uptimes = {}; // pod id -> last-seen admin uptime (drop == restart-under-webview)

const bar = document.getElementById("bar");
const content = document.getElementById("content");

// ---- boot -------------------------------------------------------------------
async function boot() {
  pods = await invoke("list_pods");
  if (pods.length === 0) {
    mode = "empty";
    await invoke("focus_chrome");
    render();
    scan(); // fire-and-forget; populates the "Detected pods" list
  } else {
    // Reopen the pod last viewed, if it still exists.
    let last = null;
    try {
      last = await invoke("last_active");
    } catch (e) {}
    const start = last && pods.find((p) => p.id === last) ? last : pods[0].id;
    await activate(start);
    pollReach();
  }
  startTimers();
}

// ---- background timers ------------------------------------------------------
let timersStarted = false;
function startTimers() {
  if (timersStarted) return;
  timersStarted = true;
  setInterval(pollReach, 8000); // refresh tab liveness dots
  setInterval(autoRescan, 15000); // surface newly-up pods while in settings/empty
}

async function pollReach() {
  if (!pods.length || (mode !== "pod" && mode !== "error")) return;
  try {
    const r = await invoke("pods_reachability");
    const m = {};
    const u = {};
    r.forEach((x) => {
      m[x.id] = x.reachable;
      if (x.uptime != null) u[x.id] = x.uptime;
    });
    reach = m;

    // Any pod whose admin uptime DROPPED restarted under its webview — the
    // rendered content is from the previous process and may be stale/blank.
    // Force-reload it (a parked background webview reloads off-screen, invisibly).
    // This is the case the old bare-TCP probe could never see behind tailscale.
    for (const p of pods) {
      if (uptimes[p.id] != null && u[p.id] != null && u[p.id] < uptimes[p.id]) {
        invoke("reload_pod", { id: p.id }).catch(() => {});
      }
    }
    uptimes = u;

    // Active-pod mode transitions (health-based reachability):
    //  • a parked dead-pod overlay whose pod returned → re-activate (reloads it,
    //    clearing the error);
    //  • a shown pod that just went unreachable → drop to the overlay, which via
    //    show_pod clears the Rust "loaded" flag so it reloads on return.
    if (mode === "error" && errorPod && reach[errorPod.id] === true) {
      await activate(errorPod.id);
    } else if (mode === "pod" && activeId && reach[activeId] === false) {
      await activate(activeId);
    } else {
      renderBar();
    }
  } catch (e) {}
}

async function autoRescan() {
  if ((mode !== "settings" && mode !== "empty") || scanning) return;
  try {
    const r = await invoke("discover_pods");
    if (
      JSON.stringify(r.candidates) !== JSON.stringify(discovered) ||
      (r.tailscale_error || null) !== discoveryError
    ) {
      discovered = r.candidates;
      discoveryError = r.tailscale_error || null;
      if (mode === "settings" || mode === "empty") render();
    }
  } catch (e) {}
}

// ---- keyboard tab switching (driven by native menu accelerators) ------------
window.cycleTab = (dir) => {
  if (pods.length < 2) return;
  let i = pods.findIndex((p) => p.id === activeId);
  if (i < 0) i = 0;
  const j = (i + dir + pods.length) % pods.length;
  activate(pods[j].id);
};
window.gotoTab = (n) => {
  if (n >= 1 && n <= pods.length) activate(pods[n - 1].id);
};

// ---- discovery --------------------------------------------------------------
async function scan() {
  scanning = true;
  if (mode === "settings" || mode === "empty") render();
  try {
    const r = await invoke("discover_pods");
    discovered = r.candidates;
    discoveryError = r.tailscale_error || null;
  } catch (e) {
    discovered = [];
    discoveryError = null;
  }
  scanning = false;
  if (mode === "settings" || mode === "empty") render();
}

async function addDiscovered(c) {
  const wasEmpty = pods.length === 0;
  try {
    pods = await invoke("add_pod", { name: c.name, url: c.url });
  } catch (e) {
    return;
  }
  discovered = discovered.filter((d) => d.url !== c.url);
  if (wasEmpty) await activate(pods[pods.length - 1].id);
  else render();
}

// ---- pod activation ---------------------------------------------------------
async function activate(id) {
  const res = await invoke("show_pod", { id });
  activeId = id;
  if (res.reachable) {
    mode = "pod";
  } else {
    mode = "error";
    errorPod = { id, name: res.name };
  }
  render();
}

async function retry() {
  if (errorPod) await activate(errorPod.id);
}

// Force-reload the active pod's webview. Driven by the native "Reload Pod" (⌘R)
// menu item (eval'd by Rust) and by the restart-aware auto-reload in pollReach.
window.reloadActive = () => {
  if (activeId) invoke("reload_pod", { id: activeId }).catch(() => {});
};

// Opened from the native "Pods → Manage Pods…" menu (eval'd by Rust) and from
// the in-bar gear button.
window.openSettings = async () => {
  await invoke("focus_chrome");
  mode = "settings";
  render();
  scan();
};

async function closeSettings() {
  pods = await invoke("list_pods");
  if (pods.length === 0) {
    mode = "empty";
    await invoke("focus_chrome");
    render();
    return;
  }
  if (!pods.find((p) => p.id === activeId)) activeId = pods[0].id;
  await activate(activeId);
}

// ---- mutations --------------------------------------------------------------
async function addPod(name, url, onErr) {
  try {
    const wasEmpty = pods.length === 0;
    pods = await invoke("add_pod", { name, url });
    if (wasEmpty) {
      // First pod: jump straight into it.
      await activate(pods[pods.length - 1].id);
    } else {
      render();
    }
  } catch (e) {
    onErr(String(e));
  }
}

async function removePod(id) {
  pods = await invoke("remove_pod", { id });
  if (activeId === id) activeId = null;
  if (mode === "settings" || mode === "empty") {
    render();
  } else {
    await closeSettings();
  }
}

async function renamePod(id, name) {
  try {
    pods = await invoke("rename_pod", { id, name });
  } catch (e) {}
  render();
}

async function movePod(id, up) {
  pods = await invoke("move_pod", { id, up });
  render();
}

// ---- rendering --------------------------------------------------------------
function render() {
  renderBar();
  renderContent();
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function brand() {
  const b = el("div", "brand");
  b.appendChild(el("span", "dot"));
  b.appendChild(document.createTextNode("Evolve Pods"));
  return b;
}

function gearBtn() {
  const g = el("button", "icon-btn");
  g.title = "Manage pods";
  g.textContent = "⚙"; // gear
  g.addEventListener("click", () => window.openSettings());
  return g;
}

function renderBar() {
  bar.innerHTML = "";
  // Tab strip only exists when there is more than one pod (requirement: the
  // tab bar is hidden entirely at ≤1 pod). In settings/empty the chrome is
  // fully visible, so we still show a header for context + the gear.
  const multi = pods.length > 1;

  if (mode === "pod" || mode === "error") {
    if (multi) {
      bar.classList.remove("hidden");
      const tabs = el("div", "tabs");
      for (const p of pods) {
        const t = el("button", "tab" + (p.id === activeId ? " active" : ""));
        const state =
          reach[p.id] === true ? "on" : reach[p.id] === false ? "off" : "unknown";
        t.appendChild(el("span", "tab-dot " + state));
        t.appendChild(document.createTextNode(p.name));
        t.title = p.url;
        t.addEventListener("click", () => activate(p.id));
        tabs.appendChild(t);
      }
      bar.appendChild(tabs);
      bar.appendChild(gearBtn());
    } else {
      // ≤1 pod: no tab strip. The pod webview covers the whole window, so this
      // header is not visible anyway; management happens via the menu.
      bar.classList.add("hidden");
    }
    return;
  }

  // settings / empty / loading: show a simple header with brand + controls.
  bar.classList.remove("hidden");
  bar.appendChild(brand());
  bar.appendChild(el("div", "spacer"));
  if (mode === "settings") {
    const done = el("button", "btn", "Done");
    done.addEventListener("click", () => closeSettings());
    done.style.alignSelf = "center";
    done.style.marginRight = "4px";
    bar.appendChild(done);
  } else {
    bar.appendChild(gearBtn());
  }
}

function renderContent() {
  content.innerHTML = "";
  content.classList.toggle("full", bar.classList.contains("hidden"));

  if (mode === "pod") {
    // Covered by the pod webview; nothing to draw.
    return;
  }
  if (mode === "error") {
    content.appendChild(errorView());
    return;
  }
  if (mode === "settings" || mode === "empty") {
    content.appendChild(settingsView());
    return;
  }
}

function errorView() {
  const wrap = el("div", "center");
  wrap.appendChild(el("div", "big", `Can’t reach ${errorPod ? errorPod.name : "this pod"}`));
  const pod = pods.find((p) => p.id === (errorPod && errorPod.id));
  wrap.appendChild(el("div", "muted", pod ? pod.url : ""));
  const retryBtn = el("button", "btn primary", "Retry");
  retryBtn.addEventListener("click", () => retry());
  wrap.appendChild(retryBtn);
  return wrap;
}

function settingsView() {
  const panel = el("div", "panel");
  const empty = mode === "empty" || pods.length === 0;
  panel.appendChild(el("h1", null, empty ? "Add your first pod" : "Manage pods"));
  panel.appendChild(
    el(
      "p",
      "sub",
      empty
        ? "A pod is one Evolve admin server. Add its address to open it here. Each pod keeps its own login — nothing is shared between them."
        : "Each pod opens in its own tab with its own login. Pods are stored only on this machine; adding or removing one never touches any pod’s server."
    )
  );

  panel.appendChild(discoveryView());

  pods.forEach((p, index) => {
    const row = el("div", "pod-row");

    // reorder controls
    const moves = el("div", "moves");
    const up = el("button", "icon-btn small", "↑");
    up.title = "Move up";
    up.disabled = index === 0;
    up.addEventListener("click", () => movePod(p.id, true));
    const down = el("button", "icon-btn small", "↓");
    down.title = "Move down";
    down.disabled = index === pods.length - 1;
    down.addEventListener("click", () => movePod(p.id, false));
    moves.appendChild(up);
    moves.appendChild(down);
    row.appendChild(moves);

    // editable name + url
    const meta = el("div", "meta");
    const nameIn = el("input", "name-edit");
    nameIn.type = "text";
    nameIn.value = p.name;
    nameIn.addEventListener("keydown", (e) => {
      if (e.key === "Enter") e.target.blur();
    });
    nameIn.addEventListener("blur", () => {
      const v = nameIn.value.trim();
      if (v && v !== p.name) renamePod(p.id, v);
    });
    meta.appendChild(nameIn);
    meta.appendChild(el("div", "url", p.url));
    row.appendChild(meta);

    const rm = el("button", "btn danger", "Remove");
    rm.addEventListener("click", () => removePod(p.id));
    row.appendChild(rm);
    panel.appendChild(row);
  });

  panel.appendChild(addForm());
  return panel;
}

function discoveryView() {
  const box = el("div", "form");
  const head = el("div", "disc-head");
  head.appendChild(el("h2", null, "Detected pods"));
  const refresh = el("button", "btn", scanning ? "Scanning…" : "Scan again");
  refresh.disabled = scanning;
  refresh.addEventListener("click", () => scan());
  head.appendChild(refresh);
  box.appendChild(head);

  if (scanning) {
    box.appendChild(
      el("p", "disc-note", "Looking on this machine and your Tailscale network…")
    );
    return box;
  }
  // A failed tailnet scan is NOT the same as an empty network — say why, and
  // point at the manual-add path. Shown even when other candidates surfaced
  // (e.g. local found, tailnet couldn't be reached).
  if (discoveryError) {
    box.appendChild(el("p", "disc-warn", discoveryError));
  }
  if (!discovered.length) {
    if (!discoveryError) {
      box.appendChild(
        el("p", "disc-note", "Nothing new found. Add a pod manually below.")
      );
    }
    return box;
  }
  for (const c of discovered) {
    const row = el("div", "pod-row");
    const meta = el("div", "meta");
    const nameLine = el("div", "name");
    nameLine.appendChild(document.createTextNode(c.name));
    nameLine.appendChild(
      el("span", "badge", c.source === "tailscale" ? "Tailscale" : "This machine")
    );
    meta.appendChild(nameLine);
    meta.appendChild(el("div", "url", c.url));
    row.appendChild(meta);
    const add = el("button", "btn primary", "Add");
    add.addEventListener("click", () => addDiscovered(c));
    row.appendChild(add);
    box.appendChild(row);
  }
  return box;
}

function addForm() {
  const form = el("div", "form");
  form.appendChild(el("h2", null, "Add a pod"));

  const nameField = el("div", "field");
  nameField.appendChild(el("label", null, "Name"));
  const nameIn = el("input");
  nameIn.type = "text";
  nameIn.placeholder = "home pod";
  nameField.appendChild(nameIn);
  form.appendChild(nameField);

  const urlField = el("div", "field");
  urlField.appendChild(el("label", null, "Admin URL"));
  const urlIn = el("input");
  urlIn.type = "text";
  urlIn.placeholder = "http://localhost:5050";
  urlField.appendChild(urlIn);
  urlField.appendChild(
    el(
      "div",
      "field-hint",
      "Local pod: http://localhost:5050 · Remote pod over Tailscale: https://<host>.ts.net"
    )
  );
  form.appendChild(urlField);

  const err = el("div", "err");
  form.appendChild(err);

  const actions = el("div", "row-actions");
  const addBtn = el("button", "btn primary", "Add pod");
  const submit = () =>
    addPod(nameIn.value, urlIn.value, (msg) => {
      err.textContent = msg;
    });
  addBtn.addEventListener("click", submit);
  urlIn.addEventListener("keydown", (e) => {
    if (e.key === "Enter") submit();
  });
  actions.appendChild(addBtn);
  form.appendChild(actions);
  return form;
}

boot();
