// ════════════════════════════════════════════════════════════════════════
// Core: SPA sub-navigation
//
// Sibling to router.js — owns the within-page sub-routing surface:
//
//   - subTab(el, group, name)   : sub-route activation, parallel to nav().
//                                 Calls onSubTabActivate() at runtime
//                                 (defined elsewhere) for per-page hooks.
//   - _syncMaintMoreLabel /
//     toggleMaintMore /
//     _bindMaintMoreDismiss     : Maintenance "More ▾" overflow popover.
//                                 Tightly coupled to subTab's special-
//                                 case popover branch, so they ship as
//                                 one module.
//   - _syncMobileTopbarTitle    : responsive helper that mirrors the
//                                 active page's <h1> into the mobile
//                                 topbar slot. Called from router.js's
//                                 nav() on every navigation; lives here
//                                 because it was originally inline with
//                                 the subtab cluster and was lifted
//                                 along with it.
//
// Loaded alongside the other core/ modules immediately before the main
// inline <script>. The closing `if (document.readyState ...)` wiring
// for _bindMaintMoreDismiss runs at script-parse time, which means by
// the time the body is being constructed the listener is already armed.
// ════════════════════════════════════════════════════════════════════════

// Pull the active page's <h1> text into the mobile-topbar title slot
// (PR #1413 §a). Called from nav() on every navigation, and once at
// startup after the SPA restores the last-active page. Home has no
// <h1> — falls back to the active nav-item's label with the leading
// glyph stripped. No-op on desktop (the topbar is display:none above
// 980px, so a stale text node is invisible — we still write it so a
// resize down to phone width picks up the latest title).
function _syncMobileTopbarTitle() {
  const slot = document.getElementById('mobile-topbar-title');
  if (!slot) return;
  let title = '';
  const activePage = document.querySelector('.page.active');
  if (activePage) {
    const h1 = activePage.querySelector(':scope > h1');
    if (h1) {
      // First text node only — h1's may carry inline badges or buttons
      // (e.g. Maintenance's "More ▾" cluster) that we don't want to
      // smear into the topbar title.
      const t = h1.childNodes[0];
      title = (t && t.nodeType === Node.TEXT_NODE
        ? t.textContent : h1.textContent || ''
      ).trim();
    }
  }
  if (!title) {
    const navText = (document.querySelector('.nav-item.active')?.textContent || '').trim();
    // Strip the leading icon glyph (✦ ◈ ◎ etc.) + whitespace. The
    // regex matches "everything up to the first letter/digit".
    title = navText.replace(/^[^\p{L}\p{N}]+\s*/u, '').trim();
  }
  slot.textContent = title;
}
function subTab(el, group, name) {
  // Scope deactivation by the clicked tab's siblings rather than by
  // `#page-${group} *` (the old approach). Two reasons:
  //
  //   1. Nested subtab systems (Settings → Pod Config has its own
  //      Network/Bot/Identity row with group="config", but there's no
  //      #page-config element — the old selector found nothing and
  //      deactivation silently no-op'd, so clicking Bot left Network
  //      highlighted).
  //   2. Top-level click would also clobber NESTED subtabs at any
  //      depth (the descendant selector). Clicking "Pod Config"
  //      deactivated the Network/Bot/Identity tabs inside it.
  //
  // Scoping by `el.parentElement` (the `.subtabs` row) + its parent
  // (the container holding the matching `.subtab-page` siblings)
  // handles both cases uniformly.
  //
  // Special case (Phase 0 §4.3.c-Maintenance): when `el` lives inside a
  // `.subtab-more-popover` — the "More ▾" dropdown that folds Maintenance's
  // advanced subtabs — `el.parentElement` is the popover, not the outer
  // `.subtabs` row. Walk up to the real row so primary subtabs also get
  // deactivated, and clear the popover entries too. Close the popover after.
  const popover = el.closest && el.closest('.subtab-more-popover');
  const tabRow = popover ? (el.closest('.subtabs') || el.parentElement) : el.parentElement;
  if (tabRow) {
    tabRow.querySelectorAll(':scope > .subtab').forEach(t => t.classList.remove('active'));
    // Also clear active state on advanced subtabs inside any sibling popover
    // — works regardless of whether `el` itself came from the popover.
    tabRow.querySelectorAll(':scope > .subtab-more-cluster .subtab-more-popover > .subtab').forEach(t => t.classList.remove('active'));
  }
  const pagesContainer = tabRow ? tabRow.parentElement : null;
  if (pagesContainer) {
    pagesContainer.querySelectorAll(':scope > .subtab-page').forEach(p => p.classList.remove('active'));
  }
  el.classList.add('active');
  const target = document.getElementById(`${group}-${name}`);
  if (target) target.classList.add('active');
  localStorage.setItem(`evolve_subtab_${group}`, name);
  // Close the more-popover and resync its trigger label.
  if (popover) {
    const cluster = popover.closest('.subtab-more-cluster');
    if (cluster) {
      cluster.classList.remove('open');
      const trig = cluster.querySelector('.subtab-more-trigger');
      if (trig) trig.setAttribute('aria-expanded', 'false');
    }
  }
  if (tabRow) _syncMaintMoreLabel(tabRow);
  onSubTabActivate(group, name);
}

// Update the "More ▾" trigger label so it reflects which advanced subtab (if
// any) is currently active. Called after any subTab() click within a row that
// contains a `.subtab-more-cluster`. No-op for rows without a More cluster.
function _syncMaintMoreLabel(subtabsRow) {
  const cluster = subtabsRow.querySelector(':scope > .subtab-more-cluster');
  if (!cluster) return;
  const trigger = cluster.querySelector('.subtab-more-trigger');
  if (!trigger) return;
  const labelEl = trigger.querySelector('.maint-more-label');
  const activeAdv = cluster.querySelector('.subtab-more-popover > .subtab.active');
  if (activeAdv) {
    if (labelEl) labelEl.textContent = ': ' + activeAdv.textContent.trim();
    trigger.classList.add('active');
  } else {
    if (labelEl) labelEl.textContent = '';
    trigger.classList.remove('active');
  }
}

// Toggle the Maintenance "More ▾" popover. Click-outside and Escape close it
// — see _bindMaintMoreDismiss() below for those handlers.
function toggleMaintMore(ev) {
  if (ev && typeof ev.stopPropagation === 'function') ev.stopPropagation();
  const trigger = ev && ev.currentTarget;
  const cluster = trigger ? trigger.closest('.subtab-more-cluster') : null;
  if (!cluster) return;
  const open = cluster.classList.toggle('open');
  trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
}

// One-shot delegated dismiss: any pointer-down outside an open popover closes
// it; Escape collapses any open cluster. Idempotent — installed once at boot.
function _bindMaintMoreDismiss() {
  if (window._maintMoreBound) return;
  window._maintMoreBound = true;
  document.addEventListener('mousedown', (e) => {
    document.querySelectorAll('.subtab-more-cluster.open').forEach(cluster => {
      if (!cluster.contains(e.target)) {
        cluster.classList.remove('open');
        const trig = cluster.querySelector('.subtab-more-trigger');
        if (trig) trig.setAttribute('aria-expanded', 'false');
      }
    });
  }, true);
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    document.querySelectorAll('.subtab-more-cluster.open').forEach(cluster => {
      cluster.classList.remove('open');
      const trig = cluster.querySelector('.subtab-more-trigger');
      if (trig) trig.setAttribute('aria-expanded', 'false');
    });
  });
}
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _bindMaintMoreDismiss);
} else {
  _bindMaintMoreDismiss();
}
