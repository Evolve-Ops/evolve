(() => {
  // ── Multi-pod hub switcher (M2) ─────────────────────────────────────
  // Turns the sidebar logo block into "EVOLVE OPS · <this pod's name>"
  // and, when network.json carries sibling pods (`peers`), a dropdown to
  // navigate between them. Hydrated from GET /api/peers.
  //
  // Hard v1 invariants (kept here deliberately):
  //   * Links only — peers carry {name, adminBaseUrl}, never tokens.
  //   * No liveness/health probing of siblings (no cross-origin fetch).
  //   * Same-window navigation (plain <a href>, NOT target=_blank). The
  //     browser already holds one per-origin pairing cookie per pod, so
  //     switching transports NO credential: a paired sibling lands in its
  //     dashboard, an unpaired one on its own /pair gate.
  // Spec: internal/design-multi-pod-2026-06-11.md §3, §3.1.
  const _btn = document.getElementById('pod-switcher-btn');
  const _nameEl = document.getElementById('pod-switcher-name');
  const _chevron = document.getElementById('pod-switcher-chevron');
  const _pop = document.getElementById('pod-switcher-pop');
  if (!_btn || !_pop || !_nameEl) return;

  let _peers = [];
  let _open = false;

  const _setOpen = (open) => {
    _open = open;
    _pop.classList.toggle('is-open', open);
    if (_chevron) _chevron.classList.toggle('is-open', open);
    _btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  };
  const _close = () => _setOpen(false);

  // Window-exported per the SPA onclick-handler lesson (mirrors
  // window._pwaInstallPromptClick in pwa-install.js) — inline onclick
  // handlers must be on window or they trip the ESLint suppressions
  // baseline. Single-pod (no peers) ⇒ the label is inert, no menu.
  window._podSwitcherClick = (event) => {
    if (event) event.stopPropagation(); // don't bubble to the logo/overview nav
    if (!_peers.length) return;
    _setOpen(!_open);
  };

  // Dismiss on outside-click or Escape — standard popover behaviour.
  document.addEventListener('click', (e) => {
    if (_open && !_pop.contains(e.target) && !_btn.contains(e.target)) _close();
  });
  document.addEventListener('keydown', (e) => {
    if (_open && e.key === 'Escape') { _close(); _btn.focus(); }
  });

  const _esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));

  const _renderMenu = (curName) => {
    // A header, the current pod (marked with a check, NOT a link), then
    // each sibling as a same-window link. A dead/stale URL is just a dead
    // link — v1 runs no liveness probe, so the menu never blocks on it.
    const check = '<span class="pod-switcher-row-check" aria-hidden="true">'
      + '<svg viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></svg></span>';
    const spacer = '<span class="pod-switcher-row-spacer" aria-hidden="true"></span>';
    let html = '<div class="pod-switcher-pop-head">Switch pod</div>';
    html += '<div class="pod-switcher-row is-current" role="menuitem" aria-current="true">'
      + check + '<span class="pod-switcher-row-name">' + _esc(curName) + '</span></div>';
    for (const p of _peers) {
      if (!p || !p.adminBaseUrl) continue;
      html += '<a class="pod-switcher-row" role="menuitem" href="' + _esc(p.adminBaseUrl) + '">'
        + spacer + '<span class="pod-switcher-row-name">'
        + _esc(p.name || p.adminBaseUrl) + '</span></a>';
    }
    _pop.innerHTML = html;
  };

  const _render = (current, peers) => {
    const curName = (current && current.name) ? current.name : '';
    // Identity label — shown for single AND multi pod (the pure win).
    if (curName) {
      _nameEl.textContent = curName;
      _btn.classList.add('has-name');
    }
    _peers = Array.isArray(peers) ? peers : [];
    if (!_peers.length) return; // single-pod: static label, no chevron/dropdown
    _btn.classList.add('is-multi');
    _btn.setAttribute('aria-haspopup', 'menu');
    _renderMenu(curName);
  };

  fetch('/api/peers', { cache: 'no-store' })
    .then((r) => (r.ok ? r.json() : null))
    .then((d) => { if (d) _render(d.current, d.peers); })
    .catch(() => { /* chrome degrades to plain "EVOLVE OPS" — no-op */ });
})();
