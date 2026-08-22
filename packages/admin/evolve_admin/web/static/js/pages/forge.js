// ════════════════════════════════════════════════════════════════════════
// Page: Forge
//
// The forge-jobs surface for AI app creation. Renders the job queue
// (queued / running / awaiting_approval / approved / rejected /
// failed / complete / cancelled), per-job detail panel with the
// step-by-step plan, and the approve / reject / retry / cancel /
// dispatch actions.
//
// identity: see applications/app_identity.py::resolve_app_id (AL-1.4b,
// docs/build-AL-1.4-app-id-canonical.md §3). A ForgeJob carries `app_id` and
// `pkg_id` as two SEPARATE fields — the app being built, and the package slot
// it builds into, which for an RSI- or chat-driven build is a freshly minted
// `chat-<hex>` (analyzer/arbiter/appliers/build_app.py::_synthetic_pkg_id)
// and is deliberately not the app's id. So `job.app_id || job.pkg_id` below
// is a label fallback across two distinct job columns, not a manifest
// identity chain, and `job.pkg_id` is what /api/gallery/<pkg_id> is keyed by.
//
// Three state lets at the top of the file:
//   _forgeJobs              — last-fetched job list (12s auto-refresh)
//   _forgeSelectedJobId     — id of the job whose detail panel is open
//   _forgeAutoRefreshTimer  — setInterval handle, only set when the
//                             Forge subtab is the active surface AND
//                             at least one job is in an active status.
//
// Three lookup tables: _FORGE_STATUS_CLASS (badge style),
// _FORGE_STEP_ICON (glyph per step state), _FORGE_ACTIVE_STATUSES
// (the set of statuses that warrant auto-refresh).
//
// A trailing IIFE monkey-patches window.nav (from core/router.js) so
// _forgeManageAutoRefresh runs on every navigation. This must execute
// after core/router.js has defined window.nav — load order in
// index.html's script-tag cluster puts core/ first, then this file,
// so the capture-and-wrap is safe.
//
// Out of scope (still inline in the main script):
//   _wizardOpenForgeJob — wizard helper that opens a forge job from
//                         the bot-creation wizard surface.
//   _FORGE_STATUS_COLOR + _forgeStatusBadge — used by proposal cards
//                         to render a "forge: …" status badge inline
//                         with each proposal. Belongs with the
//                         proposal-card cluster, not the Forge page.
// ════════════════════════════════════════════════════════════════════════

// ═══════════════════════════════════════════════════════════════════════════
// FORGE JOBS
// ═══════════════════════════════════════════════════════════════════════════

let _forgeJobs = [];
let _forgeSelectedJobId = null;
let _forgeAutoRefreshTimer = null;

const _FORGE_STATUS_CLASS = {
  queued: 'badge-member',
  running: 'badge-inline',
  awaiting_approval: 'badge-approval',
  approved: 'badge-ok',
  rejected: 'badge-crit',
  failed: 'badge-crit',
  complete: 'badge-ok',
  cancelled: 'badge-member',
};

const _FORGE_STEP_ICON = {
  pending: '○',
  running: '⟳',
  done: '✓',
  failed: '✗',
  waiting: '⏳',
  cancelled: '⊘',
};

// Active statuses that warrant auto-refresh
const _FORGE_ACTIVE_STATUSES = new Set(['queued', 'running', 'approved']);

async function loadForgeJobs() {
  try {
    const d = await api('GET', '/api/forge/jobs');
    _forgeJobs = d.jobs || [];
    renderForgeJobs();
    _forgeManageAutoRefresh();
  } catch(e) {
    document.getElementById('forge-jobs-body').innerHTML =
      `<tr><td colspan="7" class="empty resp-table-fullspan" style="color:var(--red)">Failed: ${escHtml(e.message)}</td></tr>`;
  }
}

function _forgeFilterJobs(jobs) {
  const cutoff = Date.now() - 7 * 24 * 60 * 60 * 1000;
  return jobs.filter(j => {
    // 'rejected' joined this set so rejected jobs don't stick around forever
    // in the Forge Jobs page. Matches TERMINAL_JOB_STATES in forge_jobs.py.
    const terminal = j.status === 'complete' || j.status === 'failed'
                  || j.status === 'cancelled' || j.status === 'rejected';
    if (!terminal) return true;
    if (!j.created_at) return false;
    return new Date(j.created_at).getTime() > cutoff;
  });
}

function renderForgeJobs() {
  const tbody = document.getElementById('forge-jobs-body');
  const visible = _forgeFilterJobs(_forgeJobs);
  if (!visible.length) {
    tbody.innerHTML = `<tr><td colspan="9" class="empty resp-table-fullspan">No active forge jobs.</td></tr>`;
    return;
  }
  tbody.innerHTML = visible.map(j => {
    const cls = _FORGE_STATUS_CLASS[j.status] || 'badge-member';
    const totalSteps = (j.steps && j.steps.length) || 10;
    const curStep = j.current_step || 0;
    const isSelected = j.job_id === _forgeSelectedJobId;
    const started = j.created_at ? j.created_at.replace('T', ' ').slice(0, 16) : '—';
    const appLabel = j.app_id || j.pkg_id || '—';
    const jidAttr = escHtml(j.job_id);

    // Progress bar + label
    const pctDone = totalSteps > 0 ? Math.round((curStep / totalSteps) * 100) : 0;
    const barColor = j.status === 'failed' ? 'var(--red)'
                   : j.status === 'complete' ? 'var(--green)'
                   : 'var(--blue)';
    const progressHtml = `
      <div style="display:flex;align-items:center;gap:6px;min-width:100px">
        <div style="flex:1;height:5px;background:var(--bg4);border-radius:3px;overflow:hidden">
          <div style="width:${pctDone}%;height:100%;background:${barColor};border-radius:3px;transition:width 0.3s"></div>
        </div>
        <span style="font-size:0.72rem;color:var(--text2);white-space:nowrap">${curStep}/${totalSteps}</span>
      </div>`;

    // Status badge — spinner for running, tooltip with reason for rejected.
    // A complete job whose Phase 4.5 scheduled-action installs partially
    // failed (completed_with_errors, audit slate S2) renders a warn badge:
    // the app shipped but part of its schedule is not live, and green here
    // is exactly the silent-dead-app lie the flag exists to prevent.
    const _completeWithErrors = j.status === 'complete' && j.completed_with_errors;
    const spinnerHtml = j.status === 'running'
      ? `<span style="display:inline-block;animation:spin 1s linear infinite;margin-right:4px">⟳</span>`
      : '';
    const badgeTitle = (j.status === 'rejected' && j.reject_reason)
      ? ` title="${escHtml(j.reject_reason)}"`
      : _completeWithErrors
        ? ` title="The app installed, but one or more of its scheduled actions failed to set up — expand the row for detail"`
        : '';
    const badgeCls  = _completeWithErrors ? 'badge-warn' : cls;
    const badgeText = _completeWithErrors ? 'complete (errors)' : j.status;
    const statusHtml = `<span class="badge ${badgeCls}"${badgeTitle}>${spinnerHtml}${escHtml(badgeText)}</span>`;

    // Actions cell
    let actionsHtml = '';
    if (j.status === 'queued') {
      actionsHtml = `
        <button class="btn btn-green btn-sm" data-action="dispatch" data-jid="${jidAttr}" onclick="event.stopPropagation();forgeDispatch(this.dataset.jid)">▶ Dispatch</button>
        <button class="btn btn-ghost btn-sm" data-action="cancel" data-jid="${jidAttr}" onclick="event.stopPropagation();forgeCancel(this.dataset.jid)" style="color:var(--red)">Cancel</button>`;
    } else if (j.status === 'running') {
      actionsHtml = `<button class="btn btn-ghost btn-sm" data-action="cancel" data-jid="${jidAttr}" onclick="event.stopPropagation();forgeCancel(this.dataset.jid)" style="color:var(--red)">Cancel</button>`;
    } else if (j.status === 'awaiting_approval') {
      actionsHtml = `<button class="btn btn-green btn-sm" data-action="review" data-jid="${jidAttr}" onclick="event.stopPropagation();openForgePanel(this.dataset.jid)">Review →</button>`;
    } else if (j.status === 'approved') {
      actionsHtml = `<span style="font-size:0.78rem;color:var(--text2)">Applying…</span>`;
    } else if (j.status === 'complete') {
      actionsHtml = _completeWithErrors
        ? `<span style="font-size:0.78rem;color:var(--yellow)">⚠ Completed with errors</span>`
        : `<span style="font-size:0.78rem;color:var(--green)">✓ Complete</span>`;
    } else if (j.status === 'failed' || j.status === 'cancelled') {
      // Retry resets job state via POST /api/forge/jobs/<id>/retry then
      // dispatches. Preserves job_id/run_id/context so audit trail is intact.
      actionsHtml = `
        <button class="btn btn-green btn-sm" data-action="retry" data-jid="${jidAttr}" onclick="event.stopPropagation();forgeRetry(this.dataset.jid)" title="Reset to queued + dispatch">↻ Retry</button>`;
    } else if (j.status === 'rejected') {
      // Rejected jobs can also be retried — same reset path. Useful when
      // the operator rejected for a fixable reason (manifest tweak, etc.)
      // and wants another go without re-creating the job from scratch.
      actionsHtml = `
        <button class="btn btn-ghost btn-sm" data-action="retry" data-jid="${jidAttr}" onclick="event.stopPropagation();forgeRetry(this.dataset.jid)" title="Reset to queued + dispatch">↻ Retry</button>`;
    }

    // Expanded row content
    let expandHtml = '';
    if (isSelected) {
      let expandBody = '';
      if (j.status === 'queued') {
        expandBody += `<div style="font-size:0.78rem;color:var(--text2);margin-bottom:8px">Waiting to be dispatched to bot <code>${escHtml(j.bot_id ? botLabel(j.bot_id) : '—')}</code></div>`;
      }
      // "Failed before any step ran" — when status=failed and current_step
      // is 0 or 1, the steps array shows seed icons (mostly "pending" plus
      // a "waiting" on the AWAIT row) that read as live state. Surface a
      // clear "didn't run" banner so the operator isn't misled.
      const _failedWithoutRunning =
        (j.status === 'failed' || j.status === 'cancelled') &&
        (j.current_step == null || j.current_step <= 1) &&
        !(j.steps || []).some(s => s.status === 'done' || s.status === 'running' || s.status === 'failed');
      if (_failedWithoutRunning) {
        expandBody += `<div style="font-size:0.78rem;color:var(--text2);margin-bottom:8px;padding:8px 10px;background:rgba(248,113,113,0.08);border-left:3px solid var(--red);border-radius:3px">
          <div style="color:var(--red)"><strong>This job did not run.</strong></div>
          <div style="margin-top:4px">It was marked <code>${escHtml(j.status)}</code> before any step executed. The step icons below are the initial seed state, not live progress.</div>
          <div style="margin-top:4px;color:var(--text3);font-size:0.74rem">Click <strong>↻ Retry</strong> to reset to queued and dispatch.</div>
        </div>`;
      }
      if (j.status === 'rejected') {
        // Surface why the job was rejected + who rejected it. Without this,
        // operators see "rejected" with no context and have to dig through
        // logs to find out what happened.
        const reason = j.reject_reason || '(no reason recorded)';
        const who = j.rejected_by || '—';
        const when = j.rejected_at ? j.rejected_at.replace('T', ' ').slice(0, 16) : '—';
        expandBody += `<div style="font-size:0.78rem;color:var(--text2);margin-bottom:8px;padding:8px;background:var(--bg2);border-left:3px solid var(--text3);border-radius:3px">
          <div><strong>Rejected by:</strong> ${escHtml(who)} <span style="color:var(--text3);margin-left:8px">${escHtml(when)}</span></div>
          <div style="margin-top:4px"><strong>Reason:</strong> ${escHtml(reason)}</div>
        </div>`;
      }
      // Failed jobs that DID start: surface the failure step + its detail,
      // so the operator can see which step blew up without expanding the
      // full step list. _forgeRecognizeError() looks for known failure
      // patterns (missing API key, etc.) and surfaces a one-line
      // actionable hint above the raw stderr — saves the operator from
      // reading 500 chars of openclaw output to figure out the fix.
      if (j.status === 'failed' && !_failedWithoutRunning) {
        const failedStep = (j.steps || []).find(s => s.status === 'failed');
        if (failedStep) {
          const hint = _forgeRecognizeError(failedStep.detail || '', j);
          expandBody += `<div style="font-size:0.78rem;color:var(--text2);margin-bottom:8px;padding:8px;background:rgba(248,113,113,0.08);border-left:3px solid var(--red);border-radius:3px">
            <div><strong style="color:var(--red)">Step ${escHtml(String(failedStep.num))} failed:</strong> ${escHtml(failedStep.label)}</div>
            ${hint ? `<div style="margin-top:6px;padding:6px 8px;background:rgba(255,183,77,0.1);border-radius:4px;color:var(--text);font-size:0.78rem"><b style="color:var(--yellow)">Likely cause:</b> ${hint}</div>` : ''}
            ${failedStep.detail ? `<div style="margin-top:6px"><details><summary style="font-size:0.72rem;color:var(--text3);display:inline-flex;align-items:center;gap:5px"><span class="expand-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>Raw error output</summary><div style="margin-top:4px;font-family:ui-monospace,Menlo,monospace;font-size:0.72rem;color:var(--text);white-space:pre-wrap;word-break:break-word">${escHtml(failedStep.detail)}</div></details></div>` : `<div style="margin-top:4px;color:var(--text3)">No failure detail recorded — check admin server logs for job <code>${escHtml(j.job_id)}</code></div>`}
          </div>`;
        }
      }
      // Phase 4.5 partial-materialize honesty (audit slate S2): the job is
      // complete — the app's files and instructions shipped — but one or
      // more scheduled_actions[] failed to install, so those schedules will
      // never fire until remediated. Enumerate exactly which ones from the
      // per-action summary the engine stamped on context_snapshot.
      if (_completeWithErrors) {
        const _sched = (j.context_snapshot
          && Array.isArray(j.context_snapshot.scheduled_actions_installed))
          ? j.context_snapshot.scheduled_actions_installed : [];
        const _schedFailed = _sched.filter(e => e && e.status === 'failed');
        const _failRows = _schedFailed.map(e =>
          `<div style="margin-top:4px"><code>${escHtml(e.action_id || '?')}</code> <span style="color:var(--text3)">(${escHtml(e.mechanism || 'unknown')})</span> — ${escHtml(e.error || 'no diagnostic recorded')}</div>`
        ).join('');
        expandBody += `<div style="font-size:0.78rem;color:var(--text2);margin-bottom:8px;padding:8px 10px;background:rgba(251,191,36,0.08);border-left:3px solid var(--yellow);border-radius:3px">
          <div style="color:var(--yellow)"><strong>App installed, but ${_schedFailed.length} scheduled action install(s) failed.</strong></div>
          <div style="margin-top:4px">The app's files and instructions are live; the scheduled task(s) below are not — they will never fire until installed. Re-install the app to retry their setup, or fix the underlying cause first if it keeps failing.</div>
          ${_failRows}
        </div>`;
      }
      // A step rendered as "running" while the job's overall status is
      // terminal is incoherent — the job is done but the step row would
      // spin forever. The server-side janitor (sweep_orphan_steps in
      // forge_jobs.py) closes these out next sweep, but until then we
      // display the row as a special "stale" state with a one-line
      // hint. Trust j.status over per-step state for the headline.
      const _jobTerminal = (
        j.status === 'failed' || j.status === 'cancelled'
        || j.status === 'rejected' || j.status === 'complete'
      );
      if (j.steps && j.steps.length) {
        expandBody += `<div style="display:flex;flex-direction:column;gap:4px">
          ${j.steps.map(s => {
            // Special case: when the whole job didn't run, render every
            // step in muted grey regardless of seed status (avoids the
            // "step 9 looks waiting" confusion from the screenshot).
            let displayStatus = s.status;
            if (_failedWithoutRunning) displayStatus = 'pending';
            // Detect orphan running steps. The server will reconcile
            // them on the next sweep, but we render them here as
            // "stale" so the UI never shows a spinning step on a job
            // the operator already knows is done.
            const _orphan = _jobTerminal
              && !_failedWithoutRunning
              && (s.status === 'running'
                  || (s.started_at && !s.finished_at
                      && s.status !== 'done' && s.status !== 'failed'
                      && s.status !== 'cancelled'));
            if (_orphan) displayStatus = 'cancelled';
            const icon = _FORGE_STEP_ICON[displayStatus] || '○';
            const col = displayStatus === 'done' ? 'var(--green)'
                      : displayStatus === 'running' ? 'var(--blue)'
                      : displayStatus === 'failed' ? 'var(--red)'
                      : displayStatus === 'cancelled' ? '#ffb347'
                      : 'var(--text3)';
            const labelCol = (displayStatus === 'pending' || displayStatus === 'waiting')
              ? 'var(--text3)' : 'var(--text)';
            const orphanNote = _orphan
              ? ` <span style="color:#ffb347;font-size:0.72rem" title="The job's overall status is ${escHtml(j.status)}, but this step is still flagged as in-flight on disk. The next forge sweep will reconcile it.">⚠ stale — will be reconciled</span>`
              : '';
            return `<div style="display:flex;gap:8px;align-items:flex-start;font-size:0.78rem">
              <span style="color:${col};flex-shrink:0;width:14px;text-align:center">${icon}</span>
              <span style="color:${labelCol}"><strong>${escHtml(String(s.num))}.</strong> ${escHtml(s.label)}${orphanNote}</span>
              ${s.detail && !_failedWithoutRunning ? `<span style="color:var(--text3);margin-left:4px">— ${escHtml(s.detail)}</span>` : ''}
            </div>`;
          }).join('')}
        </div>`;
      }
      // Retry-chain links. When this job was cloned for retry, its
      // ``superseded_by_job_id`` points at the newer attempt; the newer
      // attempt's ``prior_job_id`` points back at this one. Render both
      // directions so the operator can navigate the chain.
      if (j.prior_job_id || j.superseded_by_job_id) {
        let chainHtml = '<div style="margin-top:10px;font-size:0.78rem;color:var(--text2);padding:6px 8px;background:var(--bg2);border-radius:3px">';
        if (j.prior_job_id) {
          const prior = escHtml(j.prior_job_id);
          chainHtml += `<div>↻ Retry of <a href="#" onclick="event.preventDefault();event.stopPropagation();toggleForgeRow('${prior}')" style="color:var(--blue);font-family:ui-monospace,Menlo,monospace">${prior}</a></div>`;
        }
        if (j.superseded_by_job_id) {
          const next = escHtml(j.superseded_by_job_id);
          chainHtml += `<div>→ Superseded by <a href="#" onclick="event.preventDefault();event.stopPropagation();toggleForgeRow('${next}')" style="color:var(--blue);font-family:ui-monospace,Menlo,monospace">${next}</a></div>`;
        }
        chainHtml += '</div>';
        expandBody += chainHtml;
      }
      if (j.status === 'awaiting_approval') {
        expandBody += `<div style="margin-top:10px"><button class="btn btn-green btn-sm" data-jid="${jidAttr}" onclick="openForgePanel(this.dataset.jid)">Review →</button></div>`;
      }
      // Manifest link — every job points at SOMETHING, but what's available
      // depends on lifecycle state:
      //   - Job that ran step 1+ → installed manifest exists in bot workspace
      //   - Job that never ran (atlas-daily-digest pattern) → only the gallery
      //     package spec exists (under {shared_dir}/gallery/imported/<pkg>.json)
      // viewForgeManifest(jobId) tries the installed manifest first and falls
      // back to the gallery spec, so this button never produces a bare "not
      // found" error on pre-install jobs.
      let manifestLink = '';
      if (j.job_id) {
        manifestLink = `
          <button class="btn btn-ghost btn-sm" data-jid="${jidAttr}" onclick="event.stopPropagation();viewForgeManifest(this.dataset.jid)" style="margin-top:10px">📄 View manifest</button>`;
      }
      expandHtml = `
      <tr id="forge-expand-${jidAttr}" style="background:var(--bg3)">
        <td colspan="9" class="resp-table-fullspan" style="padding:10px 16px">
          <div style="font-size:0.78rem;color:var(--text2);margin-bottom:8px">
            Job ID: <code>${escHtml(j.job_id)}</code> · Run: <code>${escHtml(j.run_id || '—')}</code>
          </div>
          ${expandBody}
          ${manifestLink}
        </td>
      </tr>`;
    }

    // Projected + Actual cost cells (2026-06-03 pre-install projection).
    // Projected mid is captured at job-create time; actual is summed by
    // forge_engine._reconcile_install_cost after the job completes.
    // Color the actual cell yellow at 1.5× projection (drifting) and red
    // at 2× (matches the overrun Signal threshold).
    const _fmt = (n) => `$${(Number(n) || 0).toFixed(2)}`;
    const projMid = j.projected_cost_mid_usd;
    const projHigh = j.projected_cost_high_usd;
    let projectedCell;
    if (projMid != null) {
      const rangeTitle = projHigh != null ? `range up to ${_fmt(projHigh)}` : '';
      projectedCell = `<span title="${escHtml(rangeTitle)}">${_fmt(projMid)}</span>`;
    } else {
      projectedCell = `<span style="color:var(--text3)">—</span>`;
    }
    const actual = j.actual_cost_usd;
    let actualCell;
    if (actual == null) {
      actualCell = `<span style="color:var(--text3)">—</span>`;
    } else if (projMid != null && projMid > 0) {
      const ratio = actual / projMid;
      let cellColor = 'var(--text)';
      let badge = '';
      if (ratio >= 2.0) { cellColor = 'var(--red)'; badge = ' ⚠'; }
      else if (ratio >= 1.5) { cellColor = '#ffb347'; badge = ' ↑'; }
      actualCell = `<span style="color:${cellColor};font-weight:600" title="${ratio.toFixed(2)}× projected">${_fmt(actual)}${badge}</span>`;
    } else {
      actualCell = _fmt(actual);
    }

    return `
      <tr style="cursor:pointer${isSelected ? ';background:var(--bg3)' : ''}"
          data-jid="${jidAttr}" onclick="toggleForgeRow(this.dataset.jid)">
        <td data-label="App"><strong>${escHtml(appLabel)}</strong></td>
        <td data-label="Bot">${escHtml(j.bot_id ? botLabel(j.bot_id) : '—')}</td>
        <td data-label="Type"><span class="badge badge-forge">${escHtml(j.job_type || '—')}</span></td>
        <td data-label="Progress">${progressHtml}</td>
        <td data-label="Status">${statusHtml}</td>
        <td data-label="Projected" style="font-size:0.82rem;text-align:right">${projectedCell}</td>
        <td data-label="Actual" style="font-size:0.82rem;text-align:right">${actualCell}</td>
        <td data-label="Started" style="font-size:0.78rem;color:var(--text3)">${escHtml(started)}</td>
        <td data-label="Actions" style="text-align:right;white-space:nowrap;padding:6px 10px"><div style="display:flex;gap:6px;justify-content:flex-end;align-items:center">${actionsHtml}</div></td>
      </tr>
      ${expandHtml}`;
  }).join('');
}

function toggleForgeRow(jobId) {
  const wasSelected = _forgeSelectedJobId === jobId;
  document.getElementById('forge-approval-panel').style.display = 'none';
  _forgeSelectedJobId = wasSelected ? null : jobId;
  renderForgeJobs();
}

function openForgePanel(jobId) {
  _forgeSelectedJobId = jobId;
  const job = _forgeJobs.find(j => j.job_id === jobId);
  if (!job) return;

  // Header detail
  const detail = document.getElementById('forge-approval-job-detail');
  detail.innerHTML = [
    `App: <strong>${escHtml(job.app_id || job.pkg_id || '—')}</strong>`,
    `Bot: <strong>${escHtml(job.bot_id || '—')}</strong>`,
    `Type: <strong>${escHtml(job.job_type || '—')}</strong>`,
    `ID: <code>${escHtml(job.job_id)}</code>`,
  ].join(' · ');

  // Metrics bar
  const metrics = document.getElementById('forge-approval-metrics');
  const parts = [];
  if (job.critique_rounds_done != null) parts.push(`${job.critique_rounds_done} critique round(s)`);
  if (job.issues_found != null)     parts.push(`${job.issues_found} issues found`);
  if (job.issues_resolved != null)  parts.push(`${job.issues_resolved} resolved`);
  if (job.issues_deferred != null && job.issues_deferred > 0) parts.push(`${job.issues_deferred} deferred`);
  if (job.test_exit_code != null)   parts.push(`test exit <strong>${escHtml(String(job.test_exit_code))}</strong>`);
  metrics.innerHTML = parts.join(' &nbsp;·&nbsp; ') || '<span style="color:var(--text3)">No metrics yet.</span>';

  // Test output
  const testSummaryEl = document.getElementById('forge-test-summary');
  const testIcon = document.getElementById('forge-test-icon');
  const testLabel = document.getElementById('forge-test-label');
  const testOut = document.getElementById('forge-test-output');
  if (job.test_exit_code === null || job.test_exit_code === undefined) {
    testIcon.textContent = '○';
    testIcon.style.color = 'var(--text3)';
    testLabel.textContent = 'Tests not yet run';
    testLabel.style.color = 'var(--text3)';
  } else if (job.test_exit_code === 0) {
    testIcon.textContent = '✓';
    testIcon.style.color = 'var(--green)';
    testLabel.textContent = 'Tests passed';
    testLabel.style.color = 'var(--green)';
  } else {
    testIcon.textContent = '✗';
    testIcon.style.color = 'var(--red)';
    testLabel.textContent = `Tests failed (exit ${job.test_exit_code})`;
    testLabel.style.color = 'var(--red)';
  }
  testOut.textContent = job.test_output_summary || '(no output)';

  // Generated implementation
  const implOut = document.getElementById('forge-impl-output');
  const finalImpl = job.context_snapshot && job.context_snapshot.final_impl;
  implOut.textContent = finalImpl || '(not available)';

  // Interface contract
  const contractOut = document.getElementById('forge-contract-output');
  const contract = job.context_snapshot && job.context_snapshot.interface_contract;
  contractOut.textContent = contract ? JSON.stringify(contract, null, 2) : '(not available)';

  // Reset form state
  document.getElementById('forge-approval-notes').value = '';
  document.getElementById('forge-approval-feedback').style.display = 'none';
  document.getElementById('forge-reject-form').style.display = 'none';

  document.getElementById('forge-approval-panel').style.display = 'block';
  renderForgeJobs();
  // Scroll panel into view
  setTimeout(() => document.getElementById('forge-approval-panel').scrollIntoView({behavior:'smooth', block:'nearest'}), 50);
}

function closeForgePanel() {
  document.getElementById('forge-approval-panel').style.display = 'none';
  _forgeSelectedJobId = null;
  renderForgeJobs();
}

async function forgeApprove() {
  if (!_forgeSelectedJobId) return;
  const notes = document.getElementById('forge-approval-notes').value;
  const fb = document.getElementById('forge-approval-feedback');
  fb.style.display = 'none';
  try {
    const d = await api('POST', `/api/forge/jobs/${_forgeSelectedJobId}/approve`, {
      approved_by: 'operator',
      notes,
    });
    if (d.error) {
      fb.textContent = d.error;
      fb.style.color = 'var(--red)';
      fb.style.display = 'block';
      return;
    }
    fb.textContent = 'Job approved. The bot will apply the changes.';
    fb.style.color = 'var(--green)';
    fb.style.display = 'block';
    setTimeout(() => { loadForgeJobs(); closeForgePanel(); }, 1500);
  } catch(e) {
    fb.textContent = e.message;
    fb.style.color = 'var(--red)';
    fb.style.display = 'block';
  }
}

function forgeRejectPanel() {
  document.getElementById('forge-reject-form').style.display = 'block';
  document.getElementById('forge-reject-reason').focus();
}

async function forgeRejectSubmit() {
  if (!_forgeSelectedJobId) return;
  const reason = document.getElementById('forge-reject-reason').value;
  const fb = document.getElementById('forge-approval-feedback');
  fb.style.display = 'none';
  try {
    const d = await api('POST', `/api/forge/jobs/${_forgeSelectedJobId}/reject`, {
      rejected_by: 'operator',
      reason,
    });
    if (d.error) {
      fb.textContent = d.error;
      fb.style.color = 'var(--red)';
      fb.style.display = 'block';
      return;
    }
    fb.textContent = 'Job rejected.';
    fb.style.color = 'var(--yellow)';
    fb.style.display = 'block';
    setTimeout(() => { loadForgeJobs(); closeForgePanel(); }, 1500);
  } catch(e) {
    fb.textContent = e.message;
    fb.style.color = 'var(--red)';
    fb.style.display = 'block';
  }
}

async function forgeDispatch(jobId) {
  // Optimistic feedback: find the row button and disable it
  const btn = document.querySelector(`button[data-action="dispatch"][data-jid="${CSS.escape(jobId)}"]`);
  if (btn) { btn.disabled = true; btn.textContent = 'Dispatching…'; }
  try {
    const d = await api('POST', `/api/forge/jobs/${jobId}/dispatch`);
    if (d && d.error) {
      if (btn) { btn.disabled = false; btn.textContent = '▶ Dispatch'; }
      // Show inline error near the row
      const row = document.querySelector(`tr[data-jid="${CSS.escape(jobId)}"]`);
      if (row) {
        const errCell = row.querySelector('td:last-child');
        if (errCell) errCell.innerHTML += `<span style="color:var(--red);font-size:0.72rem;margin-left:6px">${escHtml(d.error)}</span>`;
      }
      return;
    }
    loadForgeJobs();
  } catch(e) {
    if (btn) { btn.disabled = false; btn.textContent = '▶ Dispatch'; }
    const row = document.querySelector(`tr[data-jid="${CSS.escape(jobId)}"]`);
    if (row) {
      const errCell = row.querySelector('td:last-child');
      if (errCell) errCell.innerHTML += `<span style="color:var(--red);font-size:0.72rem;margin-left:6px">${escHtml(e.message)}</span>`;
    }
  }
}

async function forgeCancel(jobId) {
  if (!await confirmModal({body: 'Cancel this forge job?', danger: true})) return;
  try {
    await api('POST', `/api/forge/jobs/${jobId}/cancel`);
    loadForgeJobs();
  } catch(e) {
    const row = document.querySelector(`tr[data-jid="${CSS.escape(jobId)}"]`);
    if (row) {
      const errCell = row.querySelector('td:last-child');
      if (errCell) errCell.innerHTML += `<span style="color:var(--red);font-size:0.72rem;margin-left:6px">${escHtml(e.message)}</span>`;
    }
  }
}

// Retry clones a failed / cancelled / rejected job into a NEW job_id
// via POST /api/forge/jobs/<id>/retry, then dispatches the clone.
// Server-side path lives in clone_for_retry() (forge_jobs.py) + the
// retry endpoint in gallery_routes.py. Prior to 2026-06-05 this
// mutated the original in place via reset_to_queued() — that path
// produced incoherent state under multi-cycle retries (orphan steps
// stuck on "running" forever). The clone path returns the new
// job_id in ``d.job_id`` and the original in ``d.prior_job_id``; we
// surface the new id to the operator so they can follow the chain
// (the UI also renders prior/superseded links in the expanded row).
async function forgeRetry(jobId) {
  const btn = document.querySelector(`button[data-action="retry"][data-jid="${CSS.escape(jobId)}"]`);
  if (btn) { btn.disabled = true; btn.textContent = '↻ Retrying…'; }
  try {
    const d = await api('POST', `/api/forge/jobs/${jobId}/retry`, { dispatch: true });
    if (d && d.error) {
      if (btn) { btn.disabled = false; btn.textContent = '↻ Retry'; }
      const row = document.querySelector(`tr[data-jid="${CSS.escape(jobId)}"]`);
      if (row) {
        const errCell = row.querySelector('td:last-child');
        if (errCell) errCell.innerHTML += `<span style="color:var(--red);font-size:0.72rem;margin-left:6px">${escHtml(d.error)}</span>`;
      }
      return;
    }
    const newJobId = d && d.job_id;
    if (newJobId && newJobId !== jobId) {
      toast(`Retrying as ${newJobId} — watch the progress bar`, 'ok');
      // Surface the new clone immediately by selecting it.
      _forgeSelectedJobId = newJobId;
    } else {
      toast('Job retrying — watch the progress bar', 'ok');
    }
    loadForgeJobs();
  } catch(e) {
    if (btn) { btn.disabled = false; btn.textContent = '↻ Retry'; }
    const row = document.querySelector(`tr[data-jid="${CSS.escape(jobId)}"]`);
    if (row) {
      const errCell = row.querySelector('td:last-child');
      if (errCell) errCell.innerHTML += `<span style="color:var(--red);font-size:0.72rem;margin-left:6px">${escHtml(e.message)}</span>`;
    }
  }
}

// Pattern-match common forge failure stderr → operator-actionable hint.
//
// The bot's openclaw agent dumps verbose output when it fails. The most
// painful pattern is the missing-provider-key case (Pod_admin hit this with
// atlas-daily-digest's openai key absence) — the raw stderr is ~500
// chars and the actionable bit is one sentence buried in the middle.
//
// Add new patterns here as we see them. Each entry is [regex, hint
// string-or-function]. The hint is shown above the collapsed raw
// output in the failed-step card.
const _FORGE_ERROR_PATTERNS = [
  // "No API key found for provider 'openai'" — happens when the bot's
  // auth-profiles.json has Anthropic but the forge tries to call an
  // OpenAI-backed model. Atlas atlas-daily-digest hit this 2026-05-28.
  [/no api key found for provider ['"]?(\w+)['"]?/i, (m, job) =>
    `The bot needs a <b>${escHtml(m[1])}</b> API key but its auth-profiles.json doesn't have one. ` +
    `Add it from the bot's <a href="javascript:void(0)" onclick="document.getElementById('manifest-modal').classList.remove('open');nav(document.querySelector('.nav-item[data-page=&quot;capabilities&quot;]'))" style="color:var(--accent)">Credentials</a> page or borrow it from another bot.`,
  ],
  // "agent exited rc=1 without writing outbox file" — typically the
  // openclaw agent died before it could respond. Usually means the
  // upstream model call failed (auth, rate limit, or model not found).
  [/agent exited rc=\d+ without writing outbox file/i, () =>
    `The bot's agent died before it could respond. Check the bot's <code>~/.openclaw/agents/main/agent/auth-profiles.json</code> ` +
    `for missing credentials, and the recent admin logs for upstream API errors.`,
  ],
  // "openclaw agents add" — instruction from the openclaw CLI to fix
  // an auth issue. Surface as a direct hint.
  [/openclaw agents add <id>/i, () =>
    `The bot's openclaw config is missing an agent definition. Run <code>openclaw agents add &lt;agent_id&gt;</code> as the bot user, ` +
    `or copy auth profiles from the main agent dir.`,
  ],
];

function _forgeRecognizeError(detail, job) {
  if (!detail) return null;
  for (const [pattern, hintFn] of _FORGE_ERROR_PATTERNS) {
    const m = detail.match(pattern);
    if (m) {
      try { return typeof hintFn === 'function' ? hintFn(m, job) : hintFn; }
      catch (e) { return null; }
    }
  }
  return null;
}

// Smart manifest viewer for forge job rows.
//
// Three states a forge job can be in for "where is the manifest":
//   1. The job ran step 1+, wrote a manifest into the bot's workspace.
//      The existing /api/applications/<bot>/<app> endpoint returns it
//      and we open the standard manifest modal.
//   2. The job died before step 1 (atlas-daily-digest pattern). No
//      manifest exists in the bot workspace. But the gallery package
//      under {shared_dir}/gallery/imported/<pkg>.json IS what the
//      forge was *trying* to install — so we render that instead with
//      a header explaining the difference.
//   3. Neither exists (rare — the gallery package was deleted while a
//      job referencing it remained). Show a clear message instead of
//      a bare "not found".
//
// The previous "View manifest" button at index.html#39638 wired
// straight to viewManifest(bot, app), which 404'd for state (2) — the
// most common case for failed jobs.
async function viewForgeManifest(jobId) {
  const job = (_forgeJobs || []).find(j => j.job_id === jobId);
  if (!job) { toast('Job not found in current list', 'err'); return; }

  // State (1): installed manifest in bot workspace
  if (job.app_id && job.bot_id) {
    const installed = await api('GET', `/api/applications/${encodeURIComponent(job.bot_id)}/${encodeURIComponent(job.app_id)}`);
    if (installed && !installed.error) {
      _mBotId = job.bot_id; _mAppId = job.app_id; _mData = installed; _mEditMode = false;
      _renderManifestModal();
      document.getElementById('manifest-modal').classList.add('open');
      return;
    }
  }

  // State (2): gallery package only (job never wrote a manifest)
  if (job.pkg_id) {
    const pkg = await api('GET', `/api/gallery/${encodeURIComponent(job.pkg_id)}`);
    if (pkg && !pkg.error) {
      _showGalleryPackagePreview(job, pkg);
      return;
    }
  }

  // State (3): nothing found
  toast('No manifest available — forge job never wrote one, and the gallery package is missing too. Check {shared_dir}/gallery/ on the host.', 'err');
}

// Lightweight preview for gallery packages — used when a forge job
// hasn't yet written a bot-workspace manifest. Renders the package
// JSON in a copy-friendly view with a clear header explaining that
// this is the install target, not an installed manifest.
function _showGalleryPackagePreview(job, pkg) {
  const inner = document.getElementById('manifest-modal-inner');
  const pretty = JSON.stringify(pkg, null, 2);
  // Pull out a few well-known fields for the header summary
  const name = pkg.name || pkg.app_name || job.app_id || '—';
  const desc = pkg.description || pkg.summary || '';
  const tags = (pkg.tags || []).slice(0, 6);
  inner.innerHTML = `
    <div style="margin-bottom:14px">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:10px;margin-bottom:6px">
        <h2 style="margin:0">${escHtml(name)}</h2>
        <button class="btn btn-ghost btn-sm" onclick="document.getElementById('manifest-modal').classList.remove('open')">Close</button>
      </div>
      <div style="font-size:0.78rem;color:var(--text2)">
        Package <code>${escHtml(job.pkg_id || '—')}</code> ·
        ${tags.length ? tags.map(t => `<span class="badge" style="font-size:0.7rem">${escHtml(t)}</span>`).join(' ') : '<span style="color:var(--text3)">no tags</span>'}
      </div>
    </div>
    <div style="background:rgba(126,184,247,0.08);border:1px solid rgba(126,184,247,0.3);border-radius:6px;padding:10px 12px;font-size:0.78rem;color:var(--text2);margin-bottom:14px">
      <b>Gallery package preview.</b> This forge job didn't write an installed manifest yet — what you're looking at is the package the install was targeting. Once the forge completes step 1 successfully, this surface will switch to the bot-side installed manifest with edit affordances.
    </div>
    ${desc ? `<div style="font-size:0.85rem;margin-bottom:14px;line-height:1.5">${escHtml(desc)}</div>` : ''}
    <details style="margin-bottom:8px" open>
      <summary style="font-weight:600;font-size:0.82rem;display:flex;align-items:center;gap:8px"><span class="expand-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>Full package JSON</summary>
      <pre style="background:var(--bg3);border-radius:6px;padding:12px;font-family:ui-monospace,Menlo,monospace;font-size:0.74rem;line-height:1.5;max-height:55vh;overflow:auto;margin-top:8px">${escHtml(pretty)}</pre>
    </details>
  `;
  document.getElementById('manifest-modal').classList.add('open');
}

// Auto-refresh: poll every 12s when apps page + forge-jobs tab is active and any job is queued/running/approved
function _forgeIsTabActive() {
  // V2.2-3: forge jobs folded into Apps · Forge Jobs tab
  return document.getElementById('apps-forge-jobs')?.classList.contains('active') &&
         document.getElementById('page-apps')?.classList.contains('active');
}
function _forgeManageAutoRefresh() {
  const isActive = _forgeIsTabActive();
  const hasActiveJobs = _forgeJobs.some(j => _FORGE_ACTIVE_STATUSES.has(j.status));

  if (isActive && hasActiveJobs) {
    if (!_forgeAutoRefreshTimer) {
      _forgeAutoRefreshTimer = setInterval(() => {
        const stillActive = _forgeIsTabActive();
        if (!stillActive) {
          clearInterval(_forgeAutoRefreshTimer);
          _forgeAutoRefreshTimer = null;
          return;
        }
        loadForgeJobs();
      }, 12000);
    }
  } else {
    if (_forgeAutoRefreshTimer) {
      clearInterval(_forgeAutoRefreshTimer);
      _forgeAutoRefreshTimer = null;
    }
  }
}

// Stop auto-refresh when navigating away from forge page
(function() {
  const _origNav = window.nav;
  if (typeof _origNav === 'function') {
    window.nav = function(el) {
      _origNav(el);
      if (el?.dataset?.page !== 'forge' && _forgeAutoRefreshTimer) {
        clearInterval(_forgeAutoRefreshTimer);
        _forgeAutoRefreshTimer = null;
      }
      if (el?.dataset?.page === 'forge') {
        _forgeManageAutoRefresh();
      }
    };
  }
})();
