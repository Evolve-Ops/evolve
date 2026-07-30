// ════════════════════════════════════════════════════════════════════════
// Core: SPA router
//
// Owns the page redirect registry + the show/hide router invoked by every
// nav-item click. Loaded before the main inline script so the redirect-stub
// inline <script>nav(...)</script> tags in the body still see `nav` as a
// global at parse time (same behavior as when this code lived inline).
//
// nav() looks up onPageActivate(), _evoDrawerOnPageChange(), and friends
// via typeof guards at *call* time, so they can live in any of the other
// scripts loaded into this page — only nav itself + resolveDestination +
// pageRedirectRegistry are required globals out of this module.
// ════════════════════════════════════════════════════════════════════════

// ---------------------------------------------------------------------------
// Page redirect registry
// When a user navigates to an old page-id that has been moved/folded/renamed,
// redirect to the new location automatically.
//
// Format:
//   pageRedirectRegistry["old-page-id"] = { page: "new-page-id", subtab: "optional-sub-tab-id" }
//
// Populated incrementally across the IA rewrite:
//   V2.2-1: registry exists, no entries yet (placeholder)
//   V2.2-2: entries for renamed pages (cost → usage, etc.)
//   V2.2-3: entries for folded pages (monitoring → usage:sessions, etc.)
const pageRedirectRegistry = {
  // V2.2-3 — page foldings: old page-id → { page, subtab? }
  "capabilities": { page: "apps", subtab: "installed" },
  "gallery":      { page: "apps", subtab: "gallery" },
  "forge":        { page: "apps", subtab: "forge-jobs" },
  "monitoring":   { page: "cost", subtab: "sessions" },
  // Phase 1b: Recovery promoted to the top-level Backup page along with the
  // Cloud config that used to live at Maintenance → Backup. Old deeplinks
  // still resolve thanks to this redirect; new code points directly at Backup.
  "recovery":     { page: "backup", subtab: "recovery" },
  "modules":      { page: "settings", subtab: "modules" },
  "config":       { page: "settings", subtab: "pod-config" },
  "alerts":       { page: "maintenance", subtab: "alerts" },
  // Aliases used in pod_report / monitor deeplinks. Keep these even after
  // pod_report.py is updated — older signals already on disk still carry
  // the old paths until they're swept-resolved.
  "daemons":      { page: "maintenance", subtab: "health" },
  "liveness":     { page: "maintenance", subtab: "health" },
  "usage":        { page: "cost" },
  "tasks":        { page: "maintenance", subtab: "infrajobs" },
  // Users page consolidation (2026-05-29): the old Settings → Pod Config →
  // Identity sub-tab is now the standalone Users page.
  // See docs/spec-per-bot-users-management-2026-05-29.md.
  "identity":     { page: "users" },
};

function resolveDestination(pageId) {
  const redirect = pageRedirectRegistry[pageId];
  if (redirect) {
    return redirect;  // { page, subtab? }
  }
  return { page: pageId };
}
// ---------------------------------------------------------------------------

function nav(el) {
  const dest = resolveDestination(el.dataset.page);
  // If we're navigating AWAY from Home, write the last-seen timestamp so the
  // ribbon counters reset on next visit. We snapshot the previously-active
  // page before mutating the DOM. (Home-from-Home is a no-op intentionally.)
  const previousActive = document.querySelector('.nav-item.active');
  const previousPage = previousActive ? previousActive.dataset.page : null;
  if (previousPage === 'home' && dest.page !== 'home' && typeof _homeWriteLastSeen === 'function') {
    _homeWriteLastSeen();
  }
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  // If redirected, find the nav item for the destination page and activate it;
  // otherwise activate the clicked element.
  const destNavEl = dest.page !== el.dataset.page
    ? document.querySelector(`.nav-item[data-page="${dest.page}"]`) || el
    : el;
  destNavEl.classList.add('active');
  document.getElementById('page-' + dest.page).classList.add('active');
  localStorage.setItem('evolve_active_page', dest.page);
  onPageActivate(dest.page);
  // The chat drawer (if open) needs to swap to the new page's thread
  // and refresh its context chip. Also re-paints the FAB badge so the
  // green dot tracks whichever page the operator is now on.
  if (typeof _evoDrawerOnPageChange === 'function') _evoDrawerOnPageChange();
  if (typeof _syncMobileTopbarTitle === 'function') _syncMobileTopbarTitle();
  if (typeof _initSubtabScrollFade === 'function') _initSubtabScrollFade();
  if (dest.subtab) {
    const subtabEl = document.querySelector(`#page-${dest.page} .subtab[data-subtab="${dest.subtab}"]`);
    if (subtabEl) subtabEl.click();
  }
}
