// ════════════════════════════════════════════════════════════════════════
// Core: DOM utilities
//
// Three of the highest-fan-out helpers in the SPA:
//   - toast(msg, type)           — bottom-bar status notification
//   - escHtml(s)                 — minimal HTML-attribute-safe escape
//   - attrJsLiteral(value)       — JSON.stringify with " → &quot; so the
//                                  result survives embedding inside a
//                                  double-quoted onclick="" attribute
//
// escHtml() runs ~1,900 times across the codebase; attrJsLiteral() is the
// inert-button-bug fix any time operator text lands in an onclick literal.
// ════════════════════════════════════════════════════════════════════════

function toast(msg, type='ok') {
  const t = document.getElementById('toast');
  // Warn AND err are pinned: the operator needs time to read / copy the
  // diagnostic (lifecycle partial-success surfaces every sub-step
  // error here). A 2.8s auto-dismiss kept losing the only UI-side
  // evidence of half-state failures — the same vanishing-error problem
  // that native alert() has in the desktop webview, which is why error
  // alert() sites migrate to toast(_, 'err'). Errors stay until dismissed;
  // ok/info still auto-dismiss.
  if (type === 'warn' || type === 'err') {
    t.innerHTML = '';
    const span = document.createElement('span');
    span.textContent = msg;
    const x = document.createElement('button');
    x.type = 'button';
    x.className = 'toast-dismiss';
    x.setAttribute('aria-label', 'Dismiss');
    x.textContent = '×';
    x.onclick = () => { t.className = ''; };
    t.appendChild(span);
    t.appendChild(x);
    t.className = 'show ' + type;
    return;
  }
  t.textContent = msg; t.className = 'show ' + type;
  setTimeout(() => { t.className = ''; }, 2800);
}

// ── confirmModal — async, themed replacement for native confirm() ──────────
//
// Native window.confirm()/alert() are SILENTLY SUPPRESSED in the desktop
// Evolve Pods app (Tauri v2 + wry webview with no script-dialog delegate),
// so any destructive action gated behind `if (!confirm(msg)) return;` becomes
// a silent no-op there — and native dialogs are unthemed besides (style-guide
// violation). The desktop app navigates to the REMOTE admin SPA
// (WebviewUrl::External in desktop/src-tauri/src/main.rs), so a helper shipped
// in this SPA reaches the desktop webview for free.
//
// Returns a Promise<boolean>: true = confirmed, false = cancelled/dismissed.
// Drop-in migration:
//   if (!confirm(msg)) return;            →  if (!await confirmModal(msg)) return;
//   (the enclosing function must be `async` — almost all already are)
//
// Accepts a plain string (becomes the body) or an options object:
//   { title, body, confirmLabel='Confirm', cancelLabel='Cancel', danger=false }
// `danger:true` renders the primary action as .btn-danger (delete/revoke/reset).
// Body text is escaped; newlines become <br> so multi-line native-confirm
// strings (with \n\n) carry over unchanged.
function confirmModal(opts) {
  if (typeof opts === 'string') opts = { body: opts };
  opts = opts || {};
  const title = opts.title || 'Please confirm';
  const body = opts.body || '';
  const confirmLabel = opts.confirmLabel || 'Confirm';
  const cancelLabel = opts.cancelLabel || 'Cancel';
  const danger = !!opts.danger;
  const bodyHtml = escHtml(body).replace(/\n/g, '<br>');

  return new Promise((resolve) => {
    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay open confirm-modal-overlay';
    overlay.innerHTML =
      `<div class="modal modal-narrow" role="dialog" aria-modal="true" aria-label="${escHtml(title)}">
         <h2 style="margin-bottom:10px">${escHtml(title)}</h2>
         <div class="confirm-modal-body">${bodyHtml}</div>
         <div class="confirm-modal-actions">
           <button type="button" class="btn btn-ghost" data-cm="cancel">${escHtml(cancelLabel)}</button>
           <button type="button" class="btn ${danger ? 'btn-danger' : 'btn-primary'}" data-cm="ok">${escHtml(confirmLabel)}</button>
         </div>
       </div>`;

    let done = false;
    const finish = (result) => {
      if (done) return;
      done = true;
      document.removeEventListener('keydown', onKey, true);
      overlay.remove();
      resolve(result);
    };
    const onKey = (e) => {
      if (e.key === 'Escape') { e.preventDefault(); finish(false); }
      else if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    };

    overlay.addEventListener('click', (e) => { if (e.target === overlay) finish(false); });
    overlay.querySelector('[data-cm="cancel"]').addEventListener('click', () => finish(false));
    overlay.querySelector('[data-cm="ok"]').addEventListener('click', () => finish(true));
    document.addEventListener('keydown', onKey, true);

    document.body.appendChild(overlay);
    overlay.querySelector('[data-cm="ok"]').focus();
  });
}
// Expose as a global (same convention as the version helpers below) — page
// scripts call confirmModal() across <script> boundaries, so the assignment
// also marks it used for no-unused-vars (eslint can't see cross-file callers).
window.confirmModal = confirmModal;

function escHtml(s) { return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function _mdInline(s) {
  return escHtml(s)
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/`(.+?)`/g, '<code style="font-size:0.8rem;background:var(--bg3);padding:1px 4px;border-radius:3px">$1</code>');
}
// Embed an arbitrary JS literal inside an HTML attribute (typically a
// double-quoted ``onclick=""``). JSON.stringify produces a valid JS
// literal but its surrounding double-quotes terminate the HTML
// attribute prematurely — the browser then parses everything after as
// new attributes (and the onclick handler ends up being just the
// truncated prefix, so the button looks rendered but silently does
// nothing when clicked). Encoding the inner double-quotes as ``&quot;``
// preserves both layers: the HTML attribute survives intact, and the
// browser decodes ``&quot;`` back to ``"`` before executing the JS, so
// the handler sees a valid JS string literal.
//
// Use this anywhere you concatenate operator/user text into an onclick
// or other HTML-attribute JS argument. Bare ``JSON.stringify(value)``
// inside a double-quoted attribute is the inert-button bug.
function attrJsLiteral(value) {
  return JSON.stringify(value).replace(/"/g, '&quot;');
}

// ── Evolve version-string legibility ────────────────────────────────────────
// The canonical Evolve version is `YYYY.MMDD.PR` — the trailing component is a
// PR NUMBER, not a sequence position. A PR is numbered at creation, so one
// merged later can carry a LOWER number yet sit at the repo tip:
// v2026.0614.2884 is NEWER than v2026.0613.2885 even though 2884 < 2885.
// Recency lives in the DATE prefix (and, where the server supplies it, git
// ancestry) — never in the trailing number. These helpers foreground the date
// and demote the PR tail to a labeled build id so a human never reads it as an
// ordinal. (See the 2026-06-14 promote incident: an operator read a promote as
// a backward move because 2884 < 2885.)
const _VER_RE = /^(\d{4})\.(\d{2})(\d{2})\.(\d+)$/;
const _VER_MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                     'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

// Parse a canonical version string → {year, month, day, pr, date, short, iso}
// or null when it isn't the canonical YYYY.MMDD.PR shape (e.g. a semver
// primary version, "unknown", or "").
function verParts(version) {
  const m = _VER_RE.exec(String(version == null ? '' : version).trim());
  if (!m) return null;
  const [, y, mo, d, pr] = m;
  const mon = _VER_MONTHS[parseInt(mo, 10) - 1] || mo;
  return {
    year: y, month: mo, day: d, pr,
    date: `${mon} ${parseInt(d, 10)}, ${y}`,  // "Jun 14, 2026"
    short: `${mon} ${parseInt(d, 10)}`,        // "Jun 14"
    iso: `${y}-${mo}-${d}`,                     // "2026-06-14" (sortable)
  };
}

// Date-led label — "Jun 14, 2026". Foregrounds recency, drops the
// ordinal-looking PR tail. Non-canonical strings pass through unchanged.
function verDateLabel(version) {
  const p = verParts(version);
  return p ? p.date : String(version == null ? '' : version);
}

// Compact date badge — "Jun 14" (no year). For dense header badges / cells.
function verBadgeLabel(version) {
  const p = verParts(version);
  return p ? p.short : String(version == null ? '' : version);
}

// The PR/build identifier, clearly labeled — "#2884". A secondary identifier
// only; never present this as a sequence position.
function verBuildId(version) {
  const p = verParts(version);
  return p ? `#${p.pr}` : '';
}

// No-git recency between two version strings: compares the DATE prefix only
// (the correct, sortable part) — never the PR tail. Returns
// 'newer' | 'older' | 'same-day' | '' (unparseable). For a precise count, use
// the server-supplied commits_ahead instead.
function verDateRel(a, b) {
  const pa = verParts(a), pb = verParts(b);
  if (!pa || !pb) return '';
  if (pa.iso > pb.iso) return 'newer';
  if (pa.iso < pb.iso) return 'older';
  return 'same-day';
}

// Human "N commits ahead/newer" phrase from a server-supplied ancestry count
// (release.json / release status). `ahead` is git rev-list --count
// previous..head — the ONLY ancestry-true recency signal. Returns '' when the
// count is absent or zero so callers can fall back to the date label.
function commitsAheadPhrase(ahead) {
  const n = Number(ahead);
  if (!Number.isFinite(n) || n <= 0) return '';
  return `${n} commit${n === 1 ? '' : 's'} ahead`;
}
// Expose the cross-file version helpers as globals (same convention as
// collapsible.js) — the assignment also marks them used for no-unused-vars,
// since eslint can't see the consumers in other <script>-scoped files.
window.verParts = verParts;
window.verDateLabel = verDateLabel;
window.verBadgeLabel = verBadgeLabel;
window.verBuildId = verBuildId;
window.verDateRel = verDateRel;
window.commitsAheadPhrase = commitsAheadPhrase;
