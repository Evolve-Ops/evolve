// Collapsible-card persistence — the wiring half of the `.collapsible-card`
// primitive (CSS lives in static/css/base.css; markup convention in
// docs/style-guide.md §9.14).
//
// A collapsible card is a native `<details class="card collapsible-card">`
// carrying a stable `data-collapse-id`. The body shows by default (new
// users see the explainer); only an explicit user-collapse is remembered.
// We persist that choice across visits in localStorage under
// `evolve.collapse.<data-collapse-id>` — the SPA has no other persistence
// for this, which is the whole point of the primitive.
//
// Wiring is by `addEventListener('toggle')` (native, keyboard-accessible)
// rather than inline onclick, so there is no window-export requirement for
// the handler and Space/Enter on the summary Just Works. The one global we
// do expose, `window.wireCollapsibleCards`, lets a page re-run the pass
// after a dynamic re-render — though today every card is static markup so
// the DOMContentLoaded pass below is sufficient.
(function () {
  'use strict';

  var PREFIX = 'evolve.collapse.';

  // Apply the remembered state to one card. Storage is authoritative for
  // whichever direction the user explicitly chose, so we honor BOTH:
  //   - Default-expanded cards render WITH `open` and remember an explicit
  //     collapse — storage "closed" removes `open`.
  //   - Default-collapsed cards render WITHOUT `open` (e.g. the Overview
  //     updates-detail region toggled from the summary cell) and remember an
  //     explicit open — storage "open" adds `open`.
  // Both branches are idempotent (storage tracks the live state, so a second
  // pass is a no-op) and backward-compatible: an expanded card with stored
  // "open" already has the attribute, so setting it again changes nothing.
  // Storage access is wrapped because Safari private mode and disabled-cookies
  // browsers throw on localStorage — in that case we keep the markup default.
  function applyState(el, id) {
    try {
      var v = localStorage.getItem(PREFIX + id);
      if (v === 'closed') el.removeAttribute('open');
      else if (v === 'open') el.setAttribute('open', '');
    } catch (_e) {
      /* storage unavailable — leave the markup's default state as-is */
    }
  }

  function wireOne(el) {
    var id = el.getAttribute('data-collapse-id');
    if (!id) return;
    // Apply BEFORE wiring so the programmatic collapse on init doesn't write
    // back through our own listener (it would only re-write the same value,
    // but skipping it keeps init side-effect-free).
    applyState(el, id);
    if (el.dataset.collapseWired === '1') return;  // idempotent: wire once
    el.dataset.collapseWired = '1';
    el.addEventListener('toggle', function () {
      try {
        localStorage.setItem(PREFIX + id, el.open ? 'open' : 'closed');
      } catch (_e) {
        /* storage unavailable — collapse still works, just not remembered */
      }
    });
  }

  // Wire every collapsible card under `root` (default: whole document).
  // Safe to call repeatedly and after a partial re-render.
  function wireCollapsibleCards(root) {
    var scope = (root && root.querySelectorAll) ? root : document;
    var nodes = scope.querySelectorAll('details.collapsible-card[data-collapse-id]');
    for (var i = 0; i < nodes.length; i++) wireOne(nodes[i]);
  }

  window.wireCollapsibleCards = wireCollapsibleCards;

  // Pass 1 (synchronous): collapse cards that already parsed above this
  // script tag, before they get a chance to paint expanded.
  wireCollapsibleCards();
  // Pass 2 (after full parse): catch any cards below this script tag. The
  // `collapseWired` guard makes this a no-op for cards already handled.
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () {
      wireCollapsibleCards();
    });
  }
})();
