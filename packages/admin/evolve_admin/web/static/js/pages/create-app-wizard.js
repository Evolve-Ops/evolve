// ════════════════════════════════════════════════════════════════════════
// Page: Create App Wizard (3-step app-forge modal)
//
// The Apps page's "+ Create App" wizard. Three steps:
//   Step 1 — description + target bots + power tier
//   Step 2 — streaming generation (model → tier → tokens → cost
//            projection, with elapsed-time + phase-row indicator)
//   Step 3 — approve + dispatch to ForgeJobs
//
// The state lets (_wizardStep, _wizardSession, _wizardForgeJobs,
// _wizardLastInputs, _wizardStreamState, _wizardElapsedTimer,
// _wizardStreamAbort, _wizardStep3PollTimer, _wizardCostProjection)
// remain inline in the main script (top of the file ~line 7395) and
// continue to be readable here via script-scope across <script> tags.
// They were not moved with the functions because they're early in the
// file and removing them would create a non-contiguous extraction.
//
// Functions covered:
//   openCreateWizard, closeCreateWizard, _wizardRender
//   _wizardStep1Html, _wizardStep2Html, _wizardStep3Html
//   _wizardOpenForgeJob, _wizardStep3StopPoll, _wizardStep3StartPoll
//   _wizardCancelStream, _wizardRunStream, _wizardResumeStreamPoll,
//   _wizardApplyPolledGeneration, _wizardStreamingHtml,
//   _wizardPhaseRow, _wizardStartElapsedTimer,
//   _wizardLoadCostProjection
//   wizardBack (legacy back-to-step1 reset)
//
// Cross-file linkages preserved via runtime free-variable lookup:
//   - api(), apiStream(), _parseSseBlock() — core/api.js
//   - toast(), escHtml(), attrJsLiteral() — core/dom-utils.js
//   - loadStatus() (Overview) — refreshed after approve
//   - openForgePanel — pages/forge.js (Phase 3c; the _wizardOpenForgeJob
//                      handoff jumps to the Forge Jobs view of the job)
//   - nav() / window.nav — core/router.js
// ════════════════════════════════════════════════════════════════════════

// Create App Wizard
// ══════════════════════════════════════════════════════

function openCreateWizard() {
  _wizardStep = 1;
  _wizardSession = null;
  _wizardForgeJobs = [];
  _wizardLastInputs = null;
  _wizardStreamState = null;
  _wizardCancelStream();
  const el = document.getElementById('create-app-wizard');
  el.style.display = 'flex';
  _wizardRender();
}

function closeCreateWizard() {
  _wizardCancelStream();
  _wizardStep3StopPoll();
  const el = document.getElementById('create-app-wizard');
  el.style.display = 'none';
  _wizardStep = 1;
  _wizardSession = null;
  _wizardForgeJobs = [];
  _wizardLastInputs = null;
  _wizardStreamState = null;
  _wizardCostProjection = null;
}

function _wizardRender() {
  const title = document.getElementById('wizard-title');
  const body = document.getElementById('create-app-wizard-body');
  if (!title || !body) return;

  if (_wizardStep === 1) {
    title.textContent = 'Create a New App';
    body.innerHTML = _wizardStep1Html(_wizardLastInputs);
  } else if (_wizardStep === 'streaming') {
    const kind = _wizardStreamState?.kind === 'iterate' ? 'Refining draft' : 'Designing app';
    const ver = _wizardStreamState?.ctx?.version;
    title.textContent = ver ? `${kind} · v${ver}` : kind;
    body.innerHTML = _wizardStreamingHtml();
    _wizardStartElapsedTimer();
  } else if (_wizardStep === 2) {
    title.textContent = `Review Draft · v${_wizardSession?.draft?.version ?? ''}`;
    body.innerHTML = _wizardStep2Html();
    body.scrollTop = 0;
    // Fetch the pre-install cost projection async — the operator should see
    // the projected band before pressing Approve. See spec_routes.cost_estimate.
    if (_wizardSession?.session_id) {
      _wizardLoadCostProjection(_wizardSession.session_id, _wizardSession?.draft?.version);
    }
  } else if (_wizardStep === 3) {
    // Title is intentionally generic — the body's headline names the app.
    // Title shows "Started" or "Queued" based on dispatch state.
    const allQueued = _wizardForgeJobs.length > 0
      && _wizardForgeJobs.every(j => j.dispatched === false);
    title.textContent = allQueued ? 'App Creation Queued' : 'App Creation Started';
    body.innerHTML = _wizardStep3Html();
    _wizardStep3StartPoll();
  } else {
    _wizardStep3StopPoll();
  }
}

function _wizardStep1Html(prefill) {
  const bots = Object.keys(_statusData?.bots || {});
  const esc = escHtml;
  const selectedBots = prefill?.target_bots && prefill.target_bots.length
    ? new Set(prefill.target_bots)
    : null;  // null = check all by default
  const descVal = prefill?.description ? esc(prefill.description) : '';
  const powerChecked = prefill?.power ? 'checked' : '';
  const errorMsg = prefill?.error ? esc(prefill.error) : '';

  const botCheckboxes = bots.length
    ? bots.map(b => {
        const isChecked = selectedBots ? selectedBots.has(b) : true;
        return `
        <label style="display:flex;align-items:center;gap:8px;font-size:0.85rem;color:var(--text);cursor:pointer;margin-bottom:6px">
          <input type="checkbox" id="wiz-bot-${esc(b)}" data-bot="${esc(b)}" value="${esc(b)}" style="width:auto;cursor:pointer" ${isChecked ? 'checked' : ''}>
          ${esc(b)}
        </label>`;
      }).join('')
    : '<div style="color:var(--text3);font-size:0.82rem">No bots found.</div>';

  return `
    <div style="margin-bottom:18px">
      <div style="font-size:0.75rem;color:var(--text2);font-weight:600;text-transform:uppercase;letter-spacing:0.05em;margin-bottom:8px">Target Bots</div>
      <div id="wiz-bot-list" style="display:flex;flex-wrap:wrap;gap:4px 20px">${botCheckboxes}</div>
    </div>
    <div class="form-field" style="margin-bottom:14px">
      <label>What should this app do?</label>
      <textarea id="wiz-desc" class="input-w-text" rows="5" placeholder="Describe the app in plain language — what problem it solves, what data it stores, how it's used. The more detail, the better." style="min-height:120px;resize:vertical">${descVal}</textarea>
    </div>
    <label style="display:flex;align-items:center;gap:8px;font-size:0.82rem;color:var(--text2);cursor:pointer;margin-bottom:14px">
      <input type="checkbox" id="wiz-power" style="width:auto;cursor:pointer" ${powerChecked}>
      <span>Use Power model (tier 1) — slower, higher quality. Default is Workhorse (tier 2).</span>
    </label>
    <div id="wiz-step1-error" style="color:var(--red);font-size:0.82rem;margin-bottom:8px;${errorMsg ? '' : 'display:none'}">${errorMsg}</div>
    <div style="display:flex;gap:8px;align-items:center">
      <button class="btn btn-green" id="wiz-generate-btn" onclick="wizardGenerate()">Generate Spec →</button>
      <button class="btn btn-ghost" onclick="closeCreateWizard()">Cancel</button>
    </div>
  `;
}

function _wizardStep2Html() {
  const esc = escHtml;
  const d = _wizardSession?.draft;
  if (!d) return '<div style="color:var(--red)">No draft data.</div>';

  // App identity card
  const tags = (d.application_tags || []).map(t =>
    `<span style="display:inline-block;background:rgba(74,222,128,0.12);color:var(--green);border:1px solid rgba(74,222,128,0.3);border-radius:99px;font-size:0.72rem;padding:2px 9px;margin:2px">${esc(t)}</span>`
  ).join('');

  const testCmd = d.test_command
    ? `<div style="font-family:monospace;font-size:0.78rem;color:var(--text2);background:var(--bg3);padding:5px 9px;border-radius:5px;margin-top:8px">${esc(d.test_command)}</div>`
    : '';

  // Build spec
  const buildSpec = `
    <details open style="margin-bottom:16px;border:1px solid var(--border);border-radius:8px;overflow:hidden">
      <summary style="padding:10px 14px;cursor:pointer;font-size:0.78rem;font-weight:600;color:var(--text2);background:var(--bg3);user-select:none;list-style:none;display:flex;align-items:center;gap:8px">
        <span class="expand-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>Build Specification (what Forge will implement)
      </summary>
      <pre style="padding:12px 14px;font-size:0.78rem;line-height:1.55;white-space:pre-wrap;word-break:break-word;overflow-y:auto;max-height:300px;margin:0;background:var(--bg2);color:var(--text)">${esc(d.build_spec || '')}</pre>
    </details>`;

  // App dependencies
  let depsHtml = '';
  if (d.app_dependencies?.length) {
    depsHtml = `
      <div style="margin-bottom:16px">
        <div style="font-size:0.72rem;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:var(--text3);margin-bottom:8px">App Dependencies</div>
        ${d.app_dependencies.map(dep => `
          <div style="display:flex;align-items:flex-start;gap:6px;padding:5px 0;border-bottom:1px solid var(--border);font-size:0.82rem">
            <span style="color:var(--text)">${esc(dep.display_name || dep.app_id || dep)}</span>
            ${dep.reason ? `<span style="color:var(--text2)">— ${esc(dep.reason)}</span>` : ''}
            ${dep.required ? `<span class="badge badge-core" style="margin-left:auto">required</span>` : ''}
          </div>`).join('')}
      </div>`;
  }

  // Requirements
  const req = d.requirements || {};
  const integrations = req.integrations || [];
  const secrets = req.secrets || [];
  const packages = req.python_packages || [];
  let reqHtml = '';
  if (integrations.length || secrets.length || packages.length) {
    reqHtml = `<div style="margin-bottom:16px">
      <div style="font-size:0.72rem;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:var(--text3);margin-bottom:8px">Requirements</div>`;
    if (integrations.length) reqHtml += `
      <div style="margin-bottom:8px">
        <div style="font-size:0.75rem;color:var(--text2);margin-bottom:4px">Integrations</div>
        ${integrations.map(i => `<div style="font-size:0.82rem;padding:2px 0;color:var(--text)">${esc(typeof i === 'string' ? i : JSON.stringify(i))}</div>`).join('')}
      </div>`;
    if (secrets.length) reqHtml += `
      <div style="margin-bottom:8px">
        <div style="font-size:0.75rem;color:var(--text2);margin-bottom:4px">Secrets</div>
        ${secrets.map(s => `<div style="font-family:monospace;font-size:0.78rem;padding:2px 0;color:var(--text)">${esc(typeof s === 'string' ? s : JSON.stringify(s))}</div>`).join('')}
      </div>`;
    if (packages.length) reqHtml += `
      <div style="margin-bottom:8px">
        <div style="font-size:0.75rem;color:var(--text2);margin-bottom:4px">Python Packages</div>
        ${packages.map(p => `<div style="font-family:monospace;font-size:0.78rem;padding:2px 0;color:var(--text)">${esc(typeof p === 'string' ? p : JSON.stringify(p))}</div>`).join('')}
      </div>`;
    reqHtml += '</div>';
  }

  // Conflicts
  let conflictsHtml = '';
  if (d.conflicts?.length) {
    conflictsHtml = `
      <div style="margin-bottom:16px;background:rgba(240,180,41,0.07);border:1px solid rgba(240,180,41,0.3);border-radius:8px;padding:12px 14px">
        <div style="font-size:0.75rem;font-weight:700;color:var(--yellow);margin-bottom:6px">⚠ Potential Conflicts</div>
        <div style="font-size:0.78rem;color:var(--text2);margin-bottom:8px">These existing apps may overlap with your new app.</div>
        ${d.conflicts.map(c => `
          <div style="padding:5px 0;border-bottom:1px solid rgba(240,180,41,0.15);font-size:0.82rem">
            <span style="color:var(--text);font-weight:600">${esc(c.bot_id || '')}</span>
            ${c.bot_id && c.app_id ? ' · ' : ''}
            <span style="color:var(--yellow)">${esc(c.app_id || '')}</span>
            ${c.description ? `<div style="color:var(--text2);font-size:0.78rem;margin-top:2px">${esc(c.description)}</div>` : ''}
          </div>`).join('')}
      </div>`;
  }

  // Suggestions
  let suggestionsHtml = '';
  if (d.suggestions?.length) {
    suggestionsHtml = `
      <div style="margin-bottom:16px;background:rgba(76,201,240,0.07);border:1px solid rgba(76,201,240,0.25);border-radius:8px;padding:12px 14px">
        <div style="font-size:0.75rem;font-weight:700;color:var(--blue);margin-bottom:8px">💡 Suggestions</div>
        <ul style="margin:0;padding-left:18px;font-size:0.82rem;color:var(--text2);line-height:1.6">
          ${d.suggestions.map(s => `<li>${esc(typeof s === 'string' ? s : JSON.stringify(s))}</li>`).join('')}
        </ul>
      </div>`;
  }

  return `
    <!-- Identity card -->
    <div style="background:var(--bg3);border-radius:8px;padding:14px 16px;margin-bottom:16px">
      <div style="font-size:1.05rem;font-weight:700;color:var(--text);margin-bottom:4px">${esc(d.display_name || '')}</div>
      <div style="font-size:0.85rem;color:var(--text2);margin-bottom:8px">${esc(d.description || '')}</div>
      ${tags ? `<div style="margin-bottom:6px">${tags}</div>` : ''}
      ${testCmd}
    </div>

    ${buildSpec}
    ${depsHtml}
    ${reqHtml}
    ${conflictsHtml}
    ${suggestionsHtml}

    <!-- Pre-install cost projection (loaded async after render) -->
    <div id="wiz-cost-projection" style="margin-bottom:16px"></div>

    <!-- Sticky action bar -->
    <div style="position:sticky;bottom:0;background:var(--bg2);z-index:1;border-top:1px solid var(--border);padding:14px 0 4px;margin-top:4px;display:flex;flex-direction:column;gap:8px">
      <div style="display:flex;align-items:flex-start;gap:10px;flex-wrap:wrap">
        <button class="btn btn-ghost btn-sm" onclick="wizardBack()" style="flex-shrink:0;align-self:center">← Back</button>
        <div style="flex:1;min-width:180px">
          <textarea id="wiz-feedback" class="input-w-text" rows="1" placeholder="Request changes (optional)" style="resize:vertical;min-height:36px" onfocus="this.rows=3" onblur="if(!this.value)this.rows=1"></textarea>
        </div>
        <div style="display:flex;gap:8px;flex-shrink:0;align-self:center">
          <button class="btn btn-ghost btn-sm" id="wiz-iterate-btn" onclick="wizardIterate()">Iterate →</button>
          <button class="btn btn-green btn-sm" id="wiz-approve-btn" onclick="wizardApprove()">Approve &amp; Build ✓</button>
        </div>
      </div>
      <label style="display:flex;align-items:center;gap:8px;font-size:0.75rem;color:var(--text3);cursor:pointer;padding-left:62px">
        <input type="checkbox" id="wiz-power-iter" style="width:auto;cursor:pointer">
        <span>Use Power model for this iteration</span>
      </label>
    </div>
    <div id="wiz-step2-error" style="color:var(--red);font-size:0.82rem;margin-top:8px;display:none"></div>
  `;
}

function _wizardStep3Html() {
  const esc = escHtml;
  const jobs = _wizardForgeJobs || [];
  const appName = _wizardSession?.draft?.display_name
    || jobs[0]?.app_id
    || 'your app';
  const bots = jobs.map(j => j.bot_id).filter(Boolean);
  const botList = bots.length
    ? bots.map(b => `<code style="font-size:0.82rem">${esc(b)}</code>`).join(', ')
    : '';

  // Any job left ``queued`` (require_explicit_dispatch=true) means the
  // operator opted into the two-step gate. Steer the headline accordingly
  // so we don't claim "building" when nothing is actually running yet.
  const allQueued = jobs.length > 0 && jobs.every(j => j.dispatched === false);
  const anyDispatched = jobs.some(j => j.dispatched !== false);

  // Aggregate projected cost across all bots so the operator sees the
  // same number they confirmed on the previous screen carried through.
  let projectedLine = '';
  const projTotal = jobs.reduce(
    (s, j) => s + (Number(j.projected_cost_mid_usd) || 0), 0
  );
  if (projTotal > 0) {
    projectedLine = `<div style="font-size:0.78rem;color:var(--text3);margin-top:4px">Projected install cost: <strong style="color:var(--text2)">$${projTotal.toFixed(2)}</strong></div>`;
  }

  let headerHtml;
  if (allQueued) {
    headerHtml = `
      <div style="text-align:center;padding:12px 0 18px">
        <div style="font-size:1.6rem;margin-bottom:8px">⏸</div>
        <div style="font-size:0.95rem;color:var(--text);font-weight:600;margin-bottom:4px">${esc(appName)} is queued for ${esc(bots.join(', '))}.</div>
        <div style="font-size:0.82rem;color:var(--text2)">Pod is configured to require explicit dispatch — review the queued job on the Forge Jobs page and click <strong>Dispatch</strong> to start the build.</div>
        ${projectedLine}
      </div>`;
  } else {
    const headline = bots.length === 1
      ? `Building ${esc(appName)} on ${esc(bots[0])}…`
      : `Building ${esc(appName)} on ${esc(bots.length)} bots…`;
    headerHtml = `
      <div style="text-align:center;padding:12px 0 18px">
        <div style="font-size:1.6rem;margin-bottom:8px">✦</div>
        <div style="font-size:0.95rem;color:var(--text);font-weight:600;margin-bottom:4px">${headline}</div>
        <div style="font-size:0.82rem;color:var(--text2)">Forge dispatch sent. You can close this window — progress continues in the Forge Jobs page.</div>
        ${projectedLine}
      </div>`;
  }

  // Per-job rows render with a live-status pill that the poll loop
  // (_wizardStep3Poll) refreshes every 3s. The status starts as 'queued'
  // (the freshly-created job state) and ticks to running / complete /
  // failed as the forge engine progresses.
  const jobsHtml = jobs.length ? `
    <div style="margin-bottom:18px">
      ${jobs.map(j => `
        <div class="wiz-job-row" data-jid="${esc(j.job_id)}"
             style="display:flex;align-items:center;gap:10px;padding:9px 12px;background:var(--bg3);border-radius:7px;margin-bottom:8px;font-size:0.85rem">
          <span style="font-weight:600;color:var(--text)">${esc(j.app_id)}</span>
          <span style="color:var(--text3)">→</span>
          <span style="color:var(--text2)">${esc(j.bot_id)}</span>
          <span class="wiz-job-status" data-jid="${esc(j.job_id)}"
                style="font-size:0.74rem;color:var(--text3)">${j.dispatched === false ? '⏸ queued — needs dispatch' : '⟳ starting…'}</span>
          <button class="btn btn-ghost btn-sm" style="margin-left:auto" data-jid="${esc(j.job_id || '')}" onclick="_wizardOpenForgeJob(this.dataset.jid)">View in Forge Jobs →</button>
        </div>`).join('')}
    </div>` : '<div style="color:var(--text3);font-size:0.85rem;margin-bottom:18px">No forge jobs returned.</div>';

  return `
    ${headerHtml}
    ${jobsHtml}
    <div style="display:flex;justify-content:center">
      <button class="btn btn-ghost" onclick="closeCreateWizard()">Close</button>
    </div>
  `;
}

// Step-3 "View in Forge Jobs →" handler. Closes the wizard, navigates to
// Apps → Forge Jobs, then polls for the newly-created job row and highlights
// it. The pre-fix handler called `nav(document.querySelector('[data-page=forge]'))`,
// but no element carries `data-page="forge"` — `nav(null)` threw silently and
// the click looked dead.
async function _wizardOpenForgeJob(jobId) {
  closeCreateWizard();
  const appsNavEl = document.querySelector('.nav-item[data-page="apps"]');
  if (appsNavEl) nav(appsNavEl);
  const subtabEl = document.querySelector('#page-apps .subtab[data-subtab="forge-jobs"]');
  if (subtabEl) subtabEl.click();
  if (!jobId) return;
  // Poll for the row to appear in the rendered table (loadForgeJobs is async).
  // ~3s budget — enough for the GET to land, short enough to not feel stuck.
  for (let i = 0; i < 30; i++) {
    await new Promise(r => setTimeout(r, 100));
    const row = document.querySelector(`#forge-jobs-body tr[data-jid="${jobId}"]`);
    if (!row) continue;
    if (_forgeSelectedJobId !== jobId) {
      _forgeSelectedJobId = jobId;
      renderForgeJobs();
    }
    const finalRow = document.querySelector(`#forge-jobs-body tr[data-jid="${jobId}"]`);
    if (finalRow && finalRow.scrollIntoView) {
      finalRow.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
    return;
  }
}

// Poll forge job status every 3s while the Step-3 modal is open. Stops
// when every job in _wizardForgeJobs has reached a terminal state OR
// when the wizard closes / navigates away from Step 3.
function _wizardStep3StopPoll() {
  if (_wizardStep3PollTimer) {
    clearInterval(_wizardStep3PollTimer);
    _wizardStep3PollTimer = null;
  }
}

function _wizardStep3StartPoll() {
  _wizardStep3StopPoll();
  if (!Array.isArray(_wizardForgeJobs) || !_wizardForgeJobs.length) return;
  const terminal = new Set(['complete', 'failed', 'cancelled', 'rejected']);
  const tick = async () => {
    if (_wizardStep !== 3) { _wizardStep3StopPoll(); return; }
    let payload;
    try {
      payload = await api('GET', '/api/forge/jobs');
    } catch (_e) {
      return; // transient; retry next interval
    }
    const byId = new Map(
      (payload?.jobs || []).map(j => [j.job_id, j])
    );
    let allDone = true;
    for (const j of _wizardForgeJobs) {
      const live = byId.get(j.job_id);
      const pill = document.querySelector(
        `.wiz-job-status[data-jid="${CSS.escape(j.job_id)}"]`
      );
      if (!pill) continue;
      if (!live) {
        pill.textContent = '? not found';
        pill.style.color = 'var(--text3)';
        continue;
      }
      const status = live.status || 'queued';
      const cur = live.current_step || 0;
      const total = (live.steps && live.steps.length) || 10;
      if (!terminal.has(status)) allDone = false;
      if (status === 'queued') {
        // ``queued`` after dispatch normally means the thread is just
        // about to call the bot — keep "starting" copy. For
        // require_explicit_dispatch jobs that haven't been dispatched,
        // hold the "needs dispatch" copy so we don't lie.
        pill.textContent = j.dispatched === false
          ? '⏸ queued — needs dispatch'
          : '⟳ starting…';
        pill.style.color = 'var(--text3)';
      } else if (status === 'running') {
        pill.textContent = `⟳ step ${cur}/${total}`;
        pill.style.color = 'var(--blue)';
      } else if (status === 'awaiting_approval') {
        pill.textContent = 'awaiting approval';
        pill.style.color = 'var(--yellow)';
      } else if (status === 'complete') {
        pill.textContent = `✓ complete (${cur}/${total})`;
        pill.style.color = 'var(--green)';
      } else if (status === 'failed' || status === 'cancelled') {
        pill.textContent = `✗ ${status} at step ${cur}/${total}`;
        pill.style.color = 'var(--red)';
      } else if (status === 'rejected') {
        pill.textContent = '✗ rejected';
        pill.style.color = 'var(--red)';
      } else {
        pill.textContent = status;
        pill.style.color = 'var(--text2)';
      }
    }
    if (allDone) _wizardStep3StopPoll();
  };
  // Kick once immediately so the operator sees real status without
  // waiting 3s on the first frame; then on the regular cadence.
  tick();
  _wizardStep3PollTimer = setInterval(tick, 3000);
}

async function wizardGenerate() {
  const errEl = document.getElementById('wiz-step1-error');
  if (errEl) errEl.style.display = 'none';

  const selectedBots = Array.from(
    document.querySelectorAll('#wiz-bot-list input[type=checkbox]:checked')
  ).map(cb => cb.dataset.bot).filter(Boolean);
  if (!selectedBots.length) {
    if (errEl) { errEl.textContent = 'Select at least one bot.'; errEl.style.display = ''; }
    return;
  }
  const desc = document.getElementById('wiz-desc')?.value?.trim() || '';
  if (!desc) {
    if (errEl) { errEl.textContent = 'Please describe your app.'; errEl.style.display = ''; }
    return;
  }
  const power = !!document.getElementById('wiz-power')?.checked;

  _wizardLastInputs = { description: desc, target_bots: selectedBots, power };
  _wizardStreamState = {
    kind: 'generate',
    ctx: { target_bots: selectedBots, power },
    phase: '',
    message: 'Connecting…',
    tier: '',
    model: '',
    tokens: 0,
    chars: 0,
    startedAt: Date.now(),
  };
  _wizardStep = 'streaming';
  _wizardRender();

  await _wizardRunStream({
    url: '/api/specs',
    body: { description: desc, target_bots: selectedBots, power },
    onDone: (payload) => {
      _wizardSession = { session_id: payload.session_id, status: payload.status, draft: payload.draft };
      _wizardLastInputs = null;
      _wizardStreamState = null;
      _wizardStep = 2;
      _wizardRender();
    },
    onError: (msg) => {
      _wizardLastInputs = { description: desc, target_bots: selectedBots, power, error: msg };
      _wizardStreamState = null;
      _wizardStep = 1;
      _wizardRender();
    },
  });
}

async function wizardIterate() {
  if (!_wizardSession?.session_id) return;

  const feedback = document.getElementById('wiz-feedback')?.value?.trim() || '';
  if (!feedback) {
    const errEl = document.getElementById('wiz-step2-error');
    if (errEl) { errEl.textContent = 'Add feedback before iterating.'; errEl.style.display = ''; }
    return;
  }
  const power = !!document.getElementById('wiz-power-iter')?.checked;
  const priorDraft = _wizardSession.draft;
  const nextVersion = (priorDraft?.version || 1) + 1;

  _wizardStreamState = {
    kind: 'iterate',
    ctx: { version: nextVersion, feedback, power, priorDraft },
    phase: '',
    message: 'Connecting…',
    tier: '',
    model: '',
    tokens: 0,
    chars: 0,
    startedAt: Date.now(),
  };
  _wizardStep = 'streaming';
  _wizardRender();

  await _wizardRunStream({
    url: `/api/specs/${_wizardSession.session_id}/iterate`,
    body: { feedback, power },
    onDone: (payload) => {
      _wizardSession = {
        session_id: payload.session_id || _wizardSession.session_id,
        status: payload.status,
        draft: payload.draft,
      };
      _wizardStreamState = null;
      _wizardStep = 2;
      _wizardRender();
    },
    onError: (msg) => {
      // Stay on the prior draft + show error in step2.
      _wizardStreamState = null;
      _wizardStep = 2;
      _wizardRender();
      const errEl = document.getElementById('wiz-step2-error');
      if (errEl) { errEl.textContent = msg || 'Iteration failed.'; errEl.style.display = ''; }
    },
  });
}

function _wizardCancelStream() {
  if (_wizardStreamAbort) {
    try { _wizardStreamAbort.abort(); } catch (e) { /* ignore */ }
    _wizardStreamAbort = null;
  }
  if (_wizardElapsedTimer) {
    clearInterval(_wizardElapsedTimer);
    _wizardElapsedTimer = null;
  }
}

// _wizardRunStream — name preserved for backwards-compat with callers;
// internally this is now a POLLING client, not an SSE consumer.
//
// The spec_routes endpoint POST /api/specs (and /iterate) was converted
// 2026-06-05 from a streaming SSE response to a background-worker job:
// POST returns session_id immediately, work runs server-side, the client
// polls GET /api/specs/<id> for progress + final draft. This decouples
// the browser tab from work completion — closing the tab, switching to
// another app, or sleeping the laptop no longer kills the generation.
//
// Caller contract is unchanged:
//   url:     POST URL (/api/specs or /api/specs/<id>/iterate)
//   body:    POST body — included verbatim
//   onDone:  called with the final draft payload when generation completes
//   onError: called with an operator-friendly message on failure
//
// The previous SSE-based implementation lives in git history if needed
// (search for `_consumeSseStream` in the 2026-06-05 commit range).
async function _wizardRunStream({ url, body, onDone, onError }) {
  _wizardCancelStream();
  _wizardStreamAbort = new AbortController();
  try {
    // Phase 1: POST to dispatch the background job. Returns quickly
    // with session_id (no streaming response held open).
    const resp = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: _wizardStreamAbort.signal,
    });
    if (!resp.ok) {
      let msg = `HTTP ${resp.status}`;
      try {
        const txt = await resp.text();
        if (txt) {
          // Try to parse as JSON for a cleaner error message
          try {
            const j = JSON.parse(txt);
            if (j && j.error) msg = j.error;
            else msg += ` — ${txt.slice(0, 300)}`;
          } catch (e) {
            msg += ` — ${txt.slice(0, 300)}`;
          }
        }
      } catch (e) { /* ignore */ }
      _wizardCancelStream();
      onError(msg);
      return;
    }
    const dispatchPayload = await resp.json();
    const sessionId = dispatchPayload.session_id;
    if (!sessionId) {
      _wizardCancelStream();
      onError('Dispatch returned no session_id.');
      return;
    }

    // Phase 2: Poll the session endpoint until the worker completes.
    // Initial state from the dispatch response.
    if (dispatchPayload.generation) {
      _wizardApplyPolledGeneration(dispatchPayload.generation);
    }

    // Track the draft count we've already shown so we know when the
    // worker appends a new one. For initial generation that starts
    // empty; for iterate it starts at the previous draft count.
    let priorDraftCount = (dispatchPayload.session?.drafts?.length)
      ?? (_wizardSession?.draft ? 1 : 0);
    // For iterate, dispatchPayload doesn't include drafts; query once.
    if (url.endsWith('/iterate')) {
      try {
        const s0 = await fetch(`/api/specs/${sessionId}`).then(r => r.json());
        priorDraftCount = Math.max(0, (s0.drafts?.length || 0) - 1);
        // -1 because the worker will append a new draft on success;
        // we want priorDraftCount to refer to "drafts before this run".
      } catch (e) { /* fall through with the rough value */ }
    }

    // Poll every 2 seconds. Cancel via _wizardCancelStream → AbortController.
    const POLL_INTERVAL_MS = 2000;

    while (true) {
      if (_wizardStreamAbort?.signal?.aborted) {
        return;
      }
      await new Promise((resolve, reject) => {
        const t = setTimeout(resolve, POLL_INTERVAL_MS);
        _wizardStreamAbort?.signal?.addEventListener('abort', () => {
          clearTimeout(t);
          reject(new DOMException('aborted', 'AbortError'));
        }, { once: true });
      });

      let sess;
      try {
        const sr = await fetch(`/api/specs/${sessionId}`, {
          signal: _wizardStreamAbort.signal,
        });
        if (!sr.ok) {
          _wizardCancelStream();
          onError(`Poll failed: HTTP ${sr.status}`);
          return;
        }
        sess = await sr.json();
      } catch (e) {
        if (e && e.name === 'AbortError') return;
        _wizardCancelStream();
        onError(`Poll failed: ${e?.message || e}`);
        return;
      }

      const gen = sess.generation || {};
      _wizardApplyPolledGeneration(gen);

      const genStatus = gen.status;
      if (genStatus === 'completed') {
        _wizardCancelStream();
        // The worker has appended the new draft to session.drafts.
        const newestDraft = (sess.drafts && sess.drafts.length)
          ? sess.drafts[sess.drafts.length - 1]
          : null;
        if (!newestDraft) {
          onError('Generation reported complete but no draft is present.');
          return;
        }
        onDone({
          session_id: sess.session_id,
          status: sess.status,
          draft: newestDraft,
        });
        return;
      }
      if (genStatus === 'failed') {
        _wizardCancelStream();
        onError(gen.error || 'Spec generation failed.');
        return;
      }
      if (genStatus === 'cancelled') {
        _wizardCancelStream();
        onError('Generation cancelled.');
        return;
      }
      // Otherwise: status is "queued" or "running" — keep polling.
    }
  } catch (e) {
    if (e && e.name === 'AbortError') return;
    _wizardCancelStream();
    onError(e?.message || String(e));
  } finally {
    _wizardStreamAbort = null;
  }
}

// Resume polling an in-flight generation when the wizard is reopened
// from a "Recent drafts" row (status gathering/iterating, generation
// queued/running). Same loop shape as _wizardRunStream's polling phase
// but no POST — we're attaching to a worker that's already running.
//
// On completion: lands on Step 2 with the newly-appended draft, same
// as wizardGenerate/wizardIterate onDone. On error: also lands on
// Step 2 with an error message if a draft exists, else Step 1.
async function _wizardResumeStreamPoll(sessionId) {
  _wizardCancelStream();
  _wizardStreamAbort = new AbortController();
  const POLL_INTERVAL_MS = 2000;
  const onDone = (payload) => {
    _wizardSession = payload;
    _wizardStreamState = null;
    _wizardStep = 2;
    _wizardRender();
  };
  const onError = (msg) => {
    _wizardStreamState = null;
    if (_wizardSession?.draft) {
      _wizardStep = 2;
      _wizardRender();
      const errEl = document.getElementById('wiz-step2-error');
      if (errEl) { errEl.textContent = msg || 'Generation failed.'; errEl.style.display = ''; }
    } else {
      _wizardLastInputs = { error: msg || 'Generation failed.' };
      _wizardStep = 1;
      _wizardRender();
    }
  };
  try {
    while (true) {
      if (_wizardStreamAbort?.signal?.aborted) return;
      await new Promise((resolve, reject) => {
        const t = setTimeout(resolve, POLL_INTERVAL_MS);
        _wizardStreamAbort?.signal?.addEventListener('abort', () => {
          clearTimeout(t);
          reject(new DOMException('aborted', 'AbortError'));
        }, { once: true });
      });
      let sess;
      try {
        const sr = await fetch(`/api/specs/${sessionId}`, {
          signal: _wizardStreamAbort.signal,
        });
        if (!sr.ok) {
          _wizardCancelStream();
          onError(`Poll failed: HTTP ${sr.status}`);
          return;
        }
        sess = await sr.json();
      } catch (e) {
        if (e && e.name === 'AbortError') return;
        _wizardCancelStream();
        onError(`Poll failed: ${e?.message || e}`);
        return;
      }
      const gen = sess.generation || {};
      _wizardApplyPolledGeneration(gen);
      const genStatus = gen.status;
      if (genStatus === 'completed') {
        _wizardCancelStream();
        const newestDraft = (sess.drafts && sess.drafts.length)
          ? sess.drafts[sess.drafts.length - 1]
          : null;
        if (!newestDraft) {
          onError('Generation reported complete but no draft is present.');
          return;
        }
        onDone({
          session_id: sess.session_id,
          status: sess.status,
          draft: newestDraft,
        });
        return;
      }
      if (genStatus === 'failed') {
        _wizardCancelStream();
        onError(gen.error || 'Spec generation failed.');
        return;
      }
      if (genStatus === 'cancelled') {
        _wizardCancelStream();
        onError('Generation cancelled.');
        return;
      }
    }
  } catch (e) {
    if (e && e.name === 'AbortError') return;
    _wizardCancelStream();
    onError(e?.message || String(e));
  } finally {
    _wizardStreamAbort = null;
  }
}

// Update the streaming-panel UI state from a polled generation block.
// Same fields the legacy SSE handler used to update directly, just
// driven by the session.generation snapshot instead of incremental
// events. Keeps the streaming-panel renderer unchanged.
function _wizardApplyPolledGeneration(gen) {
  if (!_wizardStreamState) return;
  if (!gen) return;
  _wizardStreamState.phase = gen.phase || _wizardStreamState.phase || '';
  _wizardStreamState.message = gen.message || _wizardStreamState.message || '';
  if (gen.tier) _wizardStreamState.tier = gen.tier;
  if (gen.model_full) _wizardStreamState.model = gen.model_full;
  if (typeof gen.partial_chars === 'number') {
    _wizardStreamState.chars = gen.partial_chars;
  }
  if (typeof gen.partial_tokens === 'number') {
    _wizardStreamState.tokens = gen.partial_tokens;
  }
  if (typeof gen.input_tokens === 'number') {
    _wizardStreamState.inputTokens = gen.input_tokens;
  }
  _renderWizardStreamingPanel();
  _renderWizardStreamingCounters();
}

// Generic SSE parser over a ReadableStream of bytes. Calls onEvent(name, data)
// for each `event: <name>\ndata: <json>\n\n` frame. Tolerates `:`-prefixed
// keepalive comments and multi-line `data:` blocks.
async function _consumeSseStream(stream, { onEvent }) {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let currentEvent = '';
  let dataLines = [];

  const flush = () => {
    if (!currentEvent && !dataLines.length) return;
    const eventName = currentEvent || 'message';
    const dataStr = dataLines.join('\n');
    currentEvent = '';
    dataLines = [];
    let payload = {};
    if (dataStr) {
      try { payload = JSON.parse(dataStr); }
      catch (e) { payload = { _raw: dataStr }; }
    }
    onEvent(eventName, payload);
  };

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let nlIdx;
    while ((nlIdx = buffer.indexOf('\n')) !== -1) {
      const line = buffer.slice(0, nlIdx).replace(/\r$/, '');
      buffer = buffer.slice(nlIdx + 1);
      if (line === '') {
        flush();
      } else if (line.startsWith(':')) {
        // SSE comment / keepalive — ignore
      } else if (line.startsWith('event:')) {
        currentEvent = line.slice(6).trimStart();
      } else if (line.startsWith('data:')) {
        dataLines.push(line.slice(5).trimStart());
      }
      // unknown line shapes silently ignored per SSE spec
    }
  }
  // Flush trailing frame if the connection closed without a final \n\n
  if (currentEvent || dataLines.length) flush();
}

function _wizardStreamingHtml() {
  const esc = escHtml;
  const st = _wizardStreamState || {};
  const kindLabel = st.kind === 'iterate'
    ? `Refining v${(st.ctx?.priorDraft?.version || 1)} → v${st.ctx?.version || ''}`
    : `Designing app for ${esc((st.ctx?.target_bots || []).join(', ') || 'selected bots')}`;
  const tierLabel = st.tier
    ? `${esc(st.model || '')} <span style="color:var(--text3)">(${esc(st.tier)})</span>`
    : '<span style="color:var(--text3)">resolving model…</span>';
  return `
    <div style="padding:8px 4px 0">
      <div style="font-size:0.82rem;color:var(--text2);margin-bottom:4px">${kindLabel}</div>
      <div style="font-size:0.78rem;color:var(--text3);margin-bottom:20px">${tierLabel}</div>
      <div id="wiz-phase-list" style="margin-bottom:22px">
        ${_wizardPhaseRow('context', 'Reading installed apps on target bots')}
        ${_wizardPhaseRow('model',   'Designing spec')}
        ${_wizardPhaseRow('parse',   'Validating draft')}
      </div>
      <div style="display:flex;align-items:center;gap:14px;font-size:0.78rem;color:var(--text3);margin-bottom:18px">
        <span><span id="wiz-stream-tokens">0</span> tokens</span>
        <span style="color:var(--border)">·</span>
        <span><span id="wiz-stream-elapsed">0</span>s elapsed</span>
        <span style="color:var(--border)">·</span>
        <span id="wiz-stream-message" style="color:var(--text2)">${esc(st.message || '')}</span>
      </div>
      <button class="btn btn-ghost btn-sm" onclick="wizardAbortStream()">Cancel</button>
    </div>
  `;
}

function _wizardPhaseRow(phase, label) {
  const esc = escHtml;
  const order = ['context', 'model', 'parse'];
  const cur = order.indexOf(_wizardStreamState?.phase || '');
  const me = order.indexOf(phase);
  let icon, color;
  if (cur === -1 || me > cur) { icon = '○'; color = 'var(--text3)'; }
  else if (me === cur) { icon = '◐'; color = 'var(--blue)'; }
  else { icon = '✓'; color = 'var(--green)'; }
  return `
    <div style="display:flex;align-items:center;gap:10px;padding:5px 0;font-size:0.85rem;color:${color}">
      <span style="font-family:ui-monospace,SFMono-Regular,monospace;width:14px;text-align:center">${icon}</span>
      <span>${esc(label)}</span>
    </div>
  `;
}

function _renderWizardStreamingPanel() {
  const body = document.getElementById('create-app-wizard-body');
  if (body) body.innerHTML = _wizardStreamingHtml();
  // Refresh the title since model/tier may have just arrived.
  const title = document.getElementById('wizard-title');
  const st = _wizardStreamState;
  if (title && st) {
    const kind = st.kind === 'iterate' ? 'Refining draft' : 'Designing app';
    const ver = st.ctx?.version;
    title.textContent = ver ? `${kind} · v${ver}` : kind;
  }
}

function _renderWizardStreamingCounters() {
  const tokEl = document.getElementById('wiz-stream-tokens');
  if (tokEl) {
    // Use real output_tokens if Anthropic reported them; otherwise approximate
    // from char count (4 chars ≈ 1 token) so the counter moves with every delta.
    const t = _wizardStreamState?.tokens || Math.round((_wizardStreamState?.chars || 0) / 4);
    tokEl.textContent = String(t);
  }
}

function _wizardStartElapsedTimer() {
  if (_wizardElapsedTimer) clearInterval(_wizardElapsedTimer);
  _wizardElapsedTimer = setInterval(() => {
    const el = document.getElementById('wiz-stream-elapsed');
    if (!el || !_wizardStreamState) {
      if (_wizardElapsedTimer) { clearInterval(_wizardElapsedTimer); _wizardElapsedTimer = null; }
      return;
    }
    const sec = Math.max(0, Math.floor((Date.now() - _wizardStreamState.startedAt) / 1000));
    el.textContent = String(sec);
  }, 1000);
}

function wizardAbortStream() {
  const wasGenerate = _wizardStreamState?.kind === 'generate';
  const inputs = _wizardLastInputs;
  _wizardCancelStream();
  _wizardStreamState = null;
  if (wasGenerate) {
    _wizardLastInputs = inputs ? { ...inputs, error: 'Cancelled.' } : null;
    _wizardStep = 1;
  } else {
    _wizardStep = 2;
  }
  _wizardRender();
}

async function _wizardLoadCostProjection(sessionId, version) {
  const el = document.getElementById('wiz-cost-projection');
  if (!el) return;
  el.innerHTML = `
    <div style="background:var(--bg3);border:1px solid var(--border);border-radius:8px;padding:10px 14px;font-size:0.78rem;color:var(--text3)">
      Estimating install cost…
    </div>`;
  let r;
  try {
    const v = version != null ? `?version=${version}` : '';
    r = await api('GET', `/api/specs/${sessionId}/cost_estimate${v}`);
  } catch (e) {
    r = { error: e?.message || 'estimate fetch failed' };
  }
  if (!r || r.error) {
    _wizardCostProjection = null;
    el.innerHTML = `
      <div style="background:var(--bg3);border:1px solid var(--border);border-radius:8px;padding:10px 14px;font-size:0.78rem;color:var(--text3)">
        Cost estimate unavailable (${escHtml(r?.error || 'no projection')}).
      </div>`;
    return;
  }
  _wizardCostProjection = r;
  const fmt = (n) => `$${(Number(n) || 0).toFixed(2)}`;
  const rows = (r.projections || []).map(p => {
    const warnFlag = p.exceeds_threshold
      ? ` <span style="color:var(--red);font-weight:700">⚠ over $${(Number(p.threshold_usd)||0).toFixed(0)} threshold</span>`
      : '';
    const ctxKB = Math.round((p.components?.bot_context_bytes || 0) / 1024);
    const specKB = Math.round((p.components?.build_spec_bytes || 0) / 1024);
    return `
      <tr>
        <td style="padding:4px 8px;font-weight:600">${escHtml(botLabel(p.bot_id))}</td>
        <td style="padding:4px 8px;color:var(--text2)">${escHtml(p.model || '')}</td>
        <td style="padding:4px 8px;text-align:right">${fmt(p.mid_usd)}</td>
        <td style="padding:4px 8px;text-align:right;color:var(--text3);font-size:0.75rem">${fmt(p.low_usd)} – ${fmt(p.high_usd)}</td>
        <td style="padding:4px 8px;color:var(--text3);font-size:0.75rem">spec ${specKB}KB · ctx ${ctxKB}KB${warnFlag}</td>
      </tr>`;
  }).join('');
  const totalLine = (r.projections || []).length > 1
    ? `<div style="margin-top:6px;font-size:0.82rem;color:var(--text2);text-align:right">Total mid: <strong>${fmt(r.total_mid_usd)}</strong></div>`
    : '';
  const warnBanner = r.any_exceeds_threshold
    ? `<div style="margin-top:8px;padding:8px 10px;background:rgba(239,68,68,0.07);border:1px solid rgba(239,68,68,0.25);border-radius:6px;font-size:0.78rem;color:var(--text2)">
         ⚠ Projected cost exceeds at least one bot's auto-approve threshold. Pressing Approve below confirms this spend.
       </div>`
    : '';
  el.innerHTML = `
    <div style="background:var(--bg3);border:1px solid var(--border);border-radius:8px;padding:10px 14px">
      <div style="font-size:0.78rem;font-weight:700;color:var(--text2);margin-bottom:6px">Projected install cost</div>
      <table style="width:100%;font-size:0.82rem;border-collapse:collapse"><tbody>${rows}</tbody></table>
      ${totalLine}
      ${warnBanner}
      <div style="margin-top:6px;font-size:0.7rem;color:var(--text3)">Estimate; actual cost depends on the build path. Operator-confirmed installs are exempt from <code>daily_cap_usd</code> by default.</div>
    </div>`;
}

async function wizardApprove() {
  const errEl = document.getElementById('wiz-step2-error');
  const btn = document.getElementById('wiz-approve-btn');
  const iterBtn = document.getElementById('wiz-iterate-btn');
  if (errEl) errEl.style.display = 'none';
  if (btn) { btn.disabled = true; btn.textContent = 'Creating forge jobs…'; }
  if (iterBtn) iterBtn.disabled = true;
  try {
    // confirmed=true: the projection rendered inline above; pressing
    // Approve IS the operator's cost confirmation. The 412 path on the
    // server is the fail-safe for direct API callers that didn't first
    // GET /cost_estimate; from the UI we shouldn't hit it.
    const r = await api('POST', `/api/specs/${_wizardSession.session_id}/approve`, {
      version: _wizardSession?.draft?.version ?? 1,
      confirmed: true,
    });

    if (!r || r.error) {
      if (errEl) { errEl.textContent = r?.error || 'Approval failed.'; errEl.style.display = ''; }
      return;
    }

    _wizardForgeJobs = r.forge_jobs || [];
    _wizardStep = 3;
    _wizardRender();
  } catch (e) {
    if (errEl) { errEl.textContent = e.message || 'Unexpected error'; errEl.style.display = ''; }
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Approve & Build ✓'; }
    if (iterBtn) iterBtn.disabled = false;
  }
}

function wizardBack() {
  _wizardStep = 1;
  _wizardSession = null;
  _wizardForgeJobs = [];
  _wizardRender();
}
