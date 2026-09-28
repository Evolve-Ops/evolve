// ════════════════════════════════════════════════════════════════════════
// PWA Phase 1.1.B — file drop + paste primitive
// Spec: internal/spec-pwa-2026-05-18.md §5.4.
//
// One small module backs three surfaces (evo drawer, home/chat page,
// Diagnostics card). Each surface attaches the same primitive with its
// own onFiles callback; the primitive enforces type + size client-side,
// renders chips, and uploads via the shared /api/chat-uploads endpoint.
//
// Server-side enforcement of the same allowlist + cap lives in
// packages/admin/evolve_admin/web/chat_upload_routes.py — the client
// guard is for UX, not security.
// ════════════════════════════════════════════════════════════════════════
(function() {
  const ALLOWED_MIME = new Set([
    'image/png', 'image/jpeg', 'image/gif', 'image/webp',
    'text/plain', 'text/markdown', 'application/json',
  ]);
  // Mirrors MAX_BYTES in chat_upload_routes.py. Keep in sync.
  const MAX_BYTES = 10 * 1024 * 1024;

  function _fmtSize(n) {
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  }

  function _isImage(mime) { return (mime || '').startsWith('image/'); }

  // Reuse the existing toast() global if present; fall back to console
  // so the primitive still works in test contexts that boot a stripped
  // shell.
  function _toast(msg, type) {
    if (typeof toast === 'function') {
      try { toast(msg, type || 'err'); return; } catch (_) { /* fall through */ }
    }
    console.warn('[pwa-drop]', msg);
  }

  // ── Type/size guards. Returns null when OK, else a reason string. ──
  function _rejectionReason(file) {
    const mime = (file.type || '').toLowerCase();
    if (!ALLOWED_MIME.has(mime)) {
      return 'Unsupported file type — only images, text, and JSON in v1.';
    }
    if (file.size > MAX_BYTES) {
      return 'File too large — 10 MB max.';
    }
    return null;
  }

  // ── Visual feedback on drag-enter/over/leave/drop ──
  // We count enter/leave events because dragenter fires on EVERY child
  // element the pointer crosses; without the counter the highlight
  // flickers as the user moves over interior nodes (input, button).
  function _wireHighlight(el) {
    let depth = 0;
    el.addEventListener('dragenter', (e) => {
      if (!_hasFiles(e)) return;
      e.preventDefault();
      depth += 1;
      el.classList.add('pwa-drop-active');
    });
    el.addEventListener('dragover', (e) => {
      if (!_hasFiles(e)) return;
      // Required for drop to fire; without it the browser falls back
      // to its default link/file handler.
      e.preventDefault();
      e.dataTransfer.dropEffect = 'copy';
    });
    el.addEventListener('dragleave', (e) => {
      if (!_hasFiles(e)) return;
      depth = Math.max(0, depth - 1);
      if (depth === 0) el.classList.remove('pwa-drop-active');
    });
    el.addEventListener('drop', () => {
      depth = 0;
      el.classList.remove('pwa-drop-active');
    });
  }

  // True when the drag carries actual files. Filters out dragged links
  // from other tabs, text selections, etc., so we don't preventDefault
  // their default browser behaviour outside our drop zone.
  function _hasFiles(ev) {
    const dt = ev.dataTransfer;
    if (!dt) return false;
    const types = dt.types;
    if (!types) return false;
    // ``types`` is a DOMStringList in some engines, plain array in
    // others; both expose `contains` / `includes` or iterable.
    if (typeof types.contains === 'function') return types.contains('Files');
    if (typeof types.includes === 'function') return types.includes('Files');
    return Array.from(types).indexOf('Files') >= 0;
  }

  // ── Public: attach drop handler to an element. ──
  // onFiles receives File[]; the surface decides what to do (preview +
  // upload, or upload + emit ref into a text field).
  function attachDropTarget(el, { onFiles, surface }) {
    if (!el) return;
    if (el._pwaDropAttached) return;  // idempotent
    el._pwaDropAttached = true;
    el.dataset.pwaSurface = surface;
    _wireHighlight(el);
    el.addEventListener('drop', (e) => {
      if (!_hasFiles(e)) return;  // link / text drops fall through
      e.preventDefault();
      const files = Array.from(e.dataTransfer.files || []);
      if (files.length === 0) return;
      onFiles(files);
    });
  }

  // ── Public: attach paste handler at document level, but only fire
  // when focus is in or on the surface's drop target / composer.
  // Critical: do NOT hijack paste in unrelated text inputs. ──
  function attachPasteHandler({ rootSelector, onFiles }) {
    document.addEventListener('paste', (e) => {
      if (!_focusIsInside(rootSelector)) return;
      const items = (e.clipboardData && e.clipboardData.items) || [];
      const images = [];
      for (const item of items) {
        if (item.kind === 'file' && (item.type || '').startsWith('image/')) {
          const blob = item.getAsFile();
          if (blob) images.push(blob);
        }
      }
      if (images.length === 0) return;  // plain text / JSON paste → default
      e.preventDefault();
      onFiles(images);
    });
  }

  function _focusIsInside(rootSelector) {
    const root = document.querySelector(rootSelector);
    if (!root) return false;
    const active = document.activeElement;
    // Focus inside the surface is the primary signal.
    if (active && root.contains(active)) return true;
    // Fallback: focus is on body/document (i.e. "nothing in particular")
    // AND the mouse is hovering over the surface. Catches the natural
    // "click into nothing, hover over chat, hit Cmd+V" gesture without
    // hijacking paste from an unrelated input the operator IS focused
    // on. If active is a real input/textarea somewhere else, we don't
    // intercept.
    const focusIsParked = !active || active === document.body
      || active === document.documentElement;
    if (focusIsParked && root.matches(':hover')) return true;
    return false;
  }

  // ── Public: upload one or more File/Blob objects to /api/chat-uploads.
  // Returns Promise<{attachments, errors}>. The caller renders chips
  // before calling this, then updates chip state from the result. ──
  async function pwaUploadAttachments(files, surface) {
    const form = new FormData();
    form.append('surface', surface);
    for (const f of files) {
      // Anonymous Blob from clipboard paste has no name; synthesize a
      // sensible one with the right extension.
      let name = f.name;
      if (!name) {
        const ext = (f.type || '').split('/')[1] || 'bin';
        name = `pasted-${Date.now()}.${ext}`;
      }
      form.append('file', f, name);
    }
    const resp = await fetch('/api/chat-uploads', {
      method: 'POST', body: form, credentials: 'same-origin',
    });
    let payload = {};
    try { payload = await resp.json(); } catch (_) { /* ignore */ }
    if (!resp.ok && (!payload.attachments || payload.attachments.length === 0)) {
      const detail = (payload && (payload.error || payload.detail)) || `HTTP ${resp.status}`;
      throw new Error(detail);
    }
    return payload;  // {attachments: [...], errors?: [...]}
  }

  // ── Public: chip-strip helpers. The chip model lets each surface
  // hold ON the file in memory (so the operator can remove before
  // send) and only flush the upload at send-time. Diagnostics is a
  // different shape — it uploads immediately on drop — but the chip
  // markup is the same. ──
  function makeChip(strip, file, opts) {
    opts = opts || {};
    const chip = document.createElement('span');
    chip.className = 'pwa-chip';
    chip._pwaFile = file;
    // Thumbnail for images, label-only for text/JSON.
    if (_isImage(file.type)) {
      const img = document.createElement('img');
      img.className = 'pwa-chip-thumb';
      img.alt = '';
      img.src = URL.createObjectURL(file);
      // Revoke the object URL once the image has decoded; the chip
      // holds the File ref for upload, not this rendered preview.
      img.addEventListener('load', () => URL.revokeObjectURL(img.src));
      chip.appendChild(img);
    }
    const name = document.createElement('span');
    name.className = 'pwa-chip-name';
    name.textContent = file.name || '(pasted)';
    chip.appendChild(name);
    const size = document.createElement('span');
    size.className = 'pwa-chip-size';
    size.textContent = _fmtSize(file.size);
    chip.appendChild(size);
    const x = document.createElement('button');
    x.type = 'button';
    x.className = 'pwa-chip-remove';
    x.setAttribute('aria-label', 'Remove attachment');
    x.textContent = '×';
    x.addEventListener('click', () => {
      chip.remove();
      if (opts.onRemove) opts.onRemove(file);
    });
    chip.appendChild(x);
    strip.appendChild(chip);
    return chip;
  }

  function clearChips(strip) {
    while (strip.firstChild) strip.removeChild(strip.firstChild);
  }

  // ── Public: validate-and-dispatch helper. Common entry point used
  // by every surface's onFiles callback. ──
  function acceptFiles(files, { onAccept, onReject }) {
    const accepted = [];
    for (const f of files) {
      const reason = _rejectionReason(f);
      if (reason) {
        if (onReject) onReject(f, reason);
        else _toast(reason, 'err');
        continue;
      }
      accepted.push(f);
    }
    if (accepted.length > 0 && onAccept) onAccept(accepted);
  }

  // Expose on window so the surface init code (further down) and any
  // future tests can reach the same primitive.
  window.pwaDrop = {
    attachDropTarget,
    attachPasteHandler,
    pwaUploadAttachments,
    makeChip,
    clearChips,
    acceptFiles,
    ALLOWED_MIME,
    MAX_BYTES,
    _fmtSize,  // exposed for tests
  };
})();

// ════════════════════════════════════════════════════════════════════════
// Surface wiring — evo drawer, home chat, diagnostics
// ════════════════════════════════════════════════════════════════════════
(function() {
  // Pending attachments per surface. Held client-side until chat-send
  // (evo / home) or uploaded immediately (diagnostics). Two separate
  // arrays keep the drawer's queue from leaking into the home page
  // queue — each surface has its own composer.
  const pending = { 'evo-drawer': [], 'home-chat': [] };
  window._pwaPending = pending;  // tests + send handlers read this

  // ── evo drawer ─────────────────────────────────────────────────
  function wireEvoDrawer() {
    const drawer = document.getElementById('evo-drawer');
    const strip = document.getElementById('evo-drawer-chips');
    if (!drawer || !strip) return;
    const onAccept = (files) => {
      for (const f of files) {
        pending['evo-drawer'].push(f);
        window.pwaDrop.makeChip(strip, f, {
          onRemove: (rm) => {
            const i = pending['evo-drawer'].indexOf(rm);
            if (i >= 0) pending['evo-drawer'].splice(i, 1);
          },
        });
      }
    };
    window.pwaDrop.attachDropTarget(drawer, {
      surface: 'evo-drawer',
      onFiles: (files) => window.pwaDrop.acceptFiles(files, { onAccept }),
    });
    window.pwaDrop.attachPasteHandler({
      rootSelector: '#evo-drawer',
      onFiles: (files) => window.pwaDrop.acceptFiles(files, { onAccept }),
    });
  }

  // ── home / chat page ───────────────────────────────────────────
  function wireHomeChat() {
    // The drop target is the chat column, not the whole page — we
    // don't want a drop on the Bots rail or anywhere else on Home to
    // attach to chat. ``.home-main`` wraps the chat column.
    const target = document.querySelector('#page-home .home-main');
    const strip = document.getElementById('home-chat-chips');
    if (!target || !strip) return;
    const onAccept = (files) => {
      for (const f of files) {
        pending['home-chat'].push(f);
        window.pwaDrop.makeChip(strip, f, {
          onRemove: (rm) => {
            const i = pending['home-chat'].indexOf(rm);
            if (i >= 0) pending['home-chat'].splice(i, 1);
          },
        });
      }
    };
    window.pwaDrop.attachDropTarget(target, {
      surface: 'home-chat',
      onFiles: (files) => window.pwaDrop.acceptFiles(files, { onAccept }),
    });
    window.pwaDrop.attachPasteHandler({
      rootSelector: '#page-home',
      onFiles: (files) => window.pwaDrop.acceptFiles(files, { onAccept }),
    });
  }

  // ── Diagnostics ────────────────────────────────────────────────
  // Different model: no chat-send to bundle into. Upload immediately
  // on drop and append a textual reference to the diag-note textarea
  // so the saved snapshot links to the attached file.
  function wireDiagnostics() {
    const card = document.getElementById('diag-report-card');
    const strip = document.getElementById('diag-chips');
    const note = document.getElementById('diag-note');
    if (!card || !strip || !note) return;
    const upload = async (files) => {
      // Chips render as uploading first so the operator has feedback.
      const chips = files.map(f => {
        const c = window.pwaDrop.makeChip(strip, f, { onRemove: () => {} });
        c.classList.add('uploading');
        return c;
      });
      try {
        const result = await window.pwaDrop.pwaUploadAttachments(
          files, 'diagnostics'
        );
        for (const a of result.attachments || []) {
          // Append "Attached: name — /url" line to the note. The saved
          // snapshot includes the note verbatim, so the reference
          // survives in the diagnostic record.
          const line = `Attached: ${a.filename} — ${a.url}`;
          note.value = note.value
            ? `${note.value}\n${line}`
            : line;
        }
        // Match chip → attachment so the operator can see what landed.
        chips.forEach((c, i) => {
          c.classList.remove('uploading');
          const a = (result.attachments || [])[i];
          if (!a) c.classList.add('error');
        });
        for (const err of (result.errors || [])) {
          if (typeof toast === 'function') toast(`${err.filename}: ${err.error}`, 'err');
        }
      } catch (e) {
        chips.forEach(c => { c.classList.remove('uploading'); c.classList.add('error'); });
        if (typeof toast === 'function') toast(`Upload failed: ${e.message || e}`, 'err');
      }
    };
    window.pwaDrop.attachDropTarget(card, {
      surface: 'diagnostics',
      onFiles: (files) => window.pwaDrop.acceptFiles(files, { onAccept: upload }),
    });
    window.pwaDrop.attachPasteHandler({
      rootSelector: '#diag-report-card',
      onFiles: (files) => window.pwaDrop.acceptFiles(files, { onAccept: upload }),
    });
  }

  // ── Send-time upload helper used by the chat composers. ──
  // Returns Promise<attachments[]> — empty when no pending files. On
  // failure, throws so the caller surfaces the error in the chat
  // bubble's normal error path.
  //
  // Crucial ordering: snapshot the queue, attempt upload FIRST, then
  // clear the queue + chip strip only on success. If we cleared
  // up-front and the upload threw, the operator would lose their
  // attachments with no way to retry — the strip would already be
  // empty by the time the error bubble appeared.
  async function flushPending(surface) {
    const queue = pending[surface] || [];
    if (queue.length === 0) return [];
    const files = queue.slice();
    const stripId = surface === 'evo-drawer'
      ? 'evo-drawer-chips' : 'home-chat-chips';
    const strip = document.getElementById(stripId);
    // Mark chips as in-flight so the operator sees the upload is
    // running — restored on failure, removed on success.
    const chips = strip
      ? Array.from(strip.querySelectorAll('.pwa-chip'))
      : [];
    chips.forEach(c => c.classList.add('uploading'));
    let result;
    try {
      result = await window.pwaDrop.pwaUploadAttachments(files, surface);
    } catch (err) {
      // Leave queue + chips intact so the operator can retry. Mark
      // chips as errored so the failure is visible, then re-throw to
      // the chat-send caller's catch block.
      chips.forEach(c => {
        c.classList.remove('uploading');
        c.classList.add('error');
      });
      throw err;
    }
    // Upload landed (in full or in part). Clear the queue + strip;
    // partial failures surface via toast so the operator knows which
    // file didn't make it. Surviving the "everything failed" case is
    // unlikely (the server would have thrown), but we still treat an
    // empty attachment list as success here — partial failures are
    // expected to be rare.
    pending[surface] = [];
    if (strip) window.pwaDrop.clearChips(strip);
    for (const err of (result.errors || [])) {
      if (typeof toast === 'function') toast(`${err.filename}: ${err.error}`, 'err');
    }
    return result.attachments || [];
  }
  window._pwaFlushPending = flushPending;

  // Wire on DOMContentLoaded (the drawer + home + diag DOM is present
  // at parse-time, but matching the rest of the file's init pattern
  // keeps things consistent for any future deferred-load shifts).
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
      wireEvoDrawer(); wireHomeChat(); wireDiagnostics();
    });
  } else {
    wireEvoDrawer(); wireHomeChat(); wireDiagnostics();
  }
})();
