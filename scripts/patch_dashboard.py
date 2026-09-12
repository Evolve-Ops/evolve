#!/usr/bin/env python3
"""Patch the Admin_bot dashboard HTML to add BotDetail page and sparkline chart."""
import re
from pathlib import Path

SRC = Path(__file__).parent.parent / "packages/dashboard/index.html"
content = SRC.read_text()

# ── 1. Add CSS ────────────────────────────────────────────────────────────────
NEW_CSS = """
    /* ── Bot Detail ─────────────────────────────────────────────────────── */
    .back-btn { display:inline-flex;align-items:center;gap:6px;color:var(--text2);font-size:0.85rem;cursor:pointer;margin-bottom:20px;padding:6px 0; }
    .back-btn:hover { color:var(--text); }
    .detail-header { display:flex;align-items:center;gap:20px;margin-bottom:24px; }
    .detail-score-circle { width:72px;height:72px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:1.4rem;font-weight:700;border:3px solid;flex-shrink:0; }
    canvas.sparkline { width:100%;height:60px;display:block; }
    .history-table { width:100%;border-collapse:collapse;font-size:0.82rem; }
    .history-table th { text-align:left;color:var(--text2);font-weight:400;padding:6px 10px;border-bottom:1px solid var(--border);font-size:0.75rem;text-transform:uppercase;letter-spacing:0.04em; }
    .history-table td { padding:8px 10px;border-bottom:1px solid var(--border); }
    .history-table tr:last-child td { border-bottom:none; }
    .history-table tr:hover td { background:var(--bg3); }
    .tier-bar { display:flex;height:8px;border-radius:4px;overflow:hidden;margin:6px 0; }
    .tier-bar-t1 { background:var(--green); }
    .tier-bar-t2 { background:var(--yellow); }
    .tier-bar-amb { background:var(--text2); }
    .ph-row { display:flex;align-items:flex-start;gap:12px;padding:10px 0;border-bottom:1px solid var(--border);font-size:0.83rem; }
    .ph-row:last-child { border-bottom:none; }
    .ph-date { color:var(--text2);font-size:0.75rem;white-space:nowrap; }
"""
content = content.replace(
    "    .score-f { border-color: var(--red); color: var(--red); }",
    "    .score-f { border-color: var(--red); color: var(--red); }" + NEW_CSS,
    1
)

# ── 2. Add BotDetail page HTML ────────────────────────────────────────────────
DETAIL_PAGE = """
    <!-- Bot Detail Page -->
    <div class="page" id="page-botdetail">
      <div class="back-btn" onclick="closeBotDetail()">&#8592; Back to Overview</div>
      <div class="detail-header">
        <div class="detail-score-circle score-a" id="detail-score-circle">?</div>
        <div>
          <h1 id="detail-bot-name">Bot</h1>
          <p id="detail-bot-meta" style="color:var(--text2);font-size:0.88rem;margin-top:4px"></p>
        </div>
      </div>
      <div class="grid grid-3" style="margin-bottom:24px">
        <div class="card"><div class="card-title">Score (30d)</div><div class="card-value" id="detail-score">-</div><div class="card-sub" id="detail-trend"></div></div>
        <div class="card"><div class="card-title">Maintenance ratio (7d)</div><div class="card-value" id="detail-maint">-</div><div class="card-sub">target &lt;20%</div></div>
        <div class="card"><div class="card-title">API key turns (7d)</div><div class="card-value" id="detail-api">-</div><div class="card-sub">should be 0</div></div>
      </div>
      <div class="card" style="margin-bottom:16px">
        <div class="card-title">30-day score trend</div>
        <div style="position:relative;height:60px;margin:10px 0 4px">
          <canvas class="sparkline" id="detail-sparkline"></canvas>
        </div>
        <div id="detail-sparkline-labels" style="display:flex;justify-content:space-between;font-size:0.7rem;color:var(--text2);margin-top:4px"></div>
      </div>
      <div class="card" style="margin-bottom:16px">
        <div class="card-title">Session tier breakdown (30d)</div>
        <div class="tier-bar" id="detail-tier-bar"></div>
        <div style="display:flex;gap:16px;font-size:0.78rem;margin-top:6px">
          <span><span style="color:var(--green)">&#9632;</span> Tier 1: <strong id="detail-t1-pct">-</strong></span>
          <span><span style="color:var(--yellow)">&#9632;</span> Tier 2: <strong id="detail-t2-pct">-</strong></span>
          <span><span style="color:var(--text2)">&#9632;</span> Ambiguous: <strong id="detail-amb-pct">-</strong></span>
        </div>
      </div>
      <div class="card" style="margin-bottom:16px">
        <div class="card-title">Daily metrics history</div>
        <table class="history-table">
          <thead><tr><th>Date</th><th>Score</th><th>T1 ratio</th><th>Maint ratio</th><th>Sessions</th><th>Resolutions</th><th>API turns</th></tr></thead>
          <tbody id="detail-history-rows"><tr><td colspan="7" style="color:var(--text2);text-align:center;padding:20px">Loading...</td></tr></tbody>
        </table>
      </div>
      <div class="card">
        <div class="card-title">Proposal history</div>
        <div id="detail-proposal-history">Loading...</div>
      </div>
    </div>

"""
content = content.replace("  </main>\n</div>", DETAIL_PAGE + "  </main>\n</div>", 1)

# ── 3. Add /evolve/api/metrics/:botId route registration note ──────────────────
# (route is in networkRoutes.ts — just add JS fetch)

# ── 4. Replace the showBotDetail stub ─────────────────────────────────────────
OLD_FN = """function showBotDetail(botId) {
  // TODO v0.5: navigate to BotDetail page
  alert(`Bot detail for ${botId} — coming in v0.5`);
}"""

NEW_FN = r"""// ── Bot Detail ───────────────────────────────────────────────────────────────
let _detailBotId = null;

function showBotDetail(botId) {
  _detailBotId = botId;
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.getElementById('page-botdetail').classList.add('active');
  loadBotDetail(botId);
}

function closeBotDetail() {
  _detailBotId = null;
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.getElementById('page-overview').classList.add('active');
}

async function loadBotDetail(botId) {
  let metrics = [];
  let allProposals = { pending: [], approved: [], deployed: [] };
  try {
    const [mRes, pRes] = await Promise.all([
      fetch('/evolve/api/metrics/' + botId),
      fetch('/evolve/api/proposals'),
    ]);
    if (mRes.ok) metrics = await mRes.json();
    if (pRes.ok) allProposals = await pRes.json();
  } catch(e) { console.error('Bot detail load:', e); }

  const sb = scoreboardData && scoreboardData.bots ? (scoreboardData.bots[botId] || {}) : {};
  const role = networkData && networkData.bots ? (networkData.bots[botId] || {}).role || 'member' : 'member';
  const port = networkData && networkData.bots ? (networkData.bots[botId] || {}).port || '-' : '-';

  document.getElementById('detail-bot-name').textContent = botId;
  document.getElementById('detail-bot-meta').textContent =
    'role: ' + role + ' · port: ' + port + ' · ' + metrics.length + ' days of data';

  const grade = sb.grade || '?';
  const sc = document.getElementById('detail-score-circle');
  sc.textContent = grade;
  sc.className = 'detail-score-circle ' + gradeClass(grade);

  const maint = sb.maintenance_ratio_7d;
  const maintStr = maint != null ? (maint*100).toFixed(0)+'%' : '-';
  const maintEl = document.getElementById('detail-maint');
  maintEl.textContent = maintStr;
  maintEl.className = 'card-value ' + (maint > 0.35 ? 'crit' : maint > 0.2 ? 'warn' : 'ok');

  document.getElementById('detail-score').textContent = sb.score != null ? sb.score : '-';
  const tEl = document.getElementById('detail-trend');
  tEl.textContent = trendSymbol(sb.trend) + ' ' + (sb.trend || 'stable');
  tEl.className = trendClass(sb.trend);

  const api = sb.api_key_turns_7d || 0;
  const apiEl = document.getElementById('detail-api');
  apiEl.textContent = api;
  apiEl.className = 'card-value ' + (api > 0 ? 'crit' : 'ok');

  renderSparkline(metrics);
  renderTierBreakdown(metrics);
  renderHistoryTable(metrics);
  renderProposalHistory(botId, allProposals);
}

function renderSparkline(metrics) {
  const canvas = document.getElementById('detail-sparkline');
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.parentElement.offsetWidth || 600, H = 60;
  canvas.width = W * dpr; canvas.height = H * dpr;
  ctx.scale(dpr, dpr);

  if (!metrics.length) {
    ctx.fillStyle = '#999'; ctx.font = '12px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('No data yet', W/2, H/2);
    return;
  }
  const scores = metrics.map(m => m.score || 0);
  const minS = Math.max(0, Math.min(...scores) - 5);
  const maxS = Math.min(100, Math.max(...scores) + 5);
  const range = maxS - minS || 1;
  const pts = scores.map((s, i) => ({
    x: scores.length > 1 ? (i / (scores.length-1)) * W : W/2,
    y: H - ((s-minS)/range) * (H-8) - 4,
  }));

  const grad = ctx.createLinearGradient(0,0,0,H);
  grad.addColorStop(0,'rgba(126,184,247,0.3)');
  grad.addColorStop(1,'rgba(126,184,247,0)');
  ctx.beginPath();
  ctx.moveTo(pts[0].x, H);
  pts.forEach(p => ctx.lineTo(p.x, p.y));
  ctx.lineTo(pts[pts.length-1].x, H);
  ctx.closePath(); ctx.fillStyle = grad; ctx.fill();
  ctx.beginPath();
  pts.forEach((p,i) => i===0 ? ctx.moveTo(p.x,p.y) : ctx.lineTo(p.x,p.y));
  ctx.strokeStyle = '#7eb8f7'; ctx.lineWidth = 2; ctx.stroke();

  const labels = document.getElementById('detail-sparkline-labels');
  if (metrics.length >= 2) {
    labels.innerHTML = '<span>' + (metrics[0].date||'') + '</span><span>' + (metrics[metrics.length-1].date||'') + '</span>';
  }
}

function renderTierBreakdown(metrics) {
  if (!metrics.length) {
    ['t1','t2','amb'].forEach(k => { document.getElementById('detail-'+k+'-pct').textContent = '-'; });
    document.getElementById('detail-tier-bar').innerHTML = '<div style="width:100%;background:var(--bg3)"></div>';
    return;
  }
  let t1=0,t2=0,amb=0;
  metrics.forEach(m => { t1+=m.tier1_sessions||0; t2+=m.tier2_sessions||0; amb+=m.ambiguous_sessions||0; });
  const total = t1+t2+amb || 1;
  const p1=(t1/total*100).toFixed(0), p2=(t2/total*100).toFixed(0), pa=(amb/total*100).toFixed(0);
  document.getElementById('detail-tier-bar').innerHTML =
    '<div class="tier-bar-t1" style="width:'+p1+'%"></div>' +
    '<div class="tier-bar-t2" style="width:'+p2+'%"></div>' +
    '<div class="tier-bar-amb" style="flex:1"></div>';
  document.getElementById('detail-t1-pct').textContent = p1+'%';
  document.getElementById('detail-t2-pct').textContent = p2+'%';
  document.getElementById('detail-amb-pct').textContent = pa+'%';
}

function renderHistoryTable(metrics) {
  const tbody = document.getElementById('detail-history-rows');
  if (!metrics.length) {
    tbody.innerHTML = '<tr><td colspan="7" style="color:var(--text2);text-align:center;padding:20px">No metrics data yet</td></tr>';
    return;
  }
  const sorted = metrics.slice().sort((a,b)=>(b.date||'').localeCompare(a.date||''));
  tbody.innerHTML = sorted.map(m => {
    const maint = m.maintenance_ratio_7d != null ? m.maintenance_ratio_7d : m.maintenance_ratio;
    const ms = maint != null ? (maint*100).toFixed(0)+'%' : '-';
    const mc = maint > 0.35 ? 'crit' : maint > 0.2 ? 'warn' : 'ok';
    const t1r = m.tier1_ratio != null ? (m.tier1_ratio*100).toFixed(0)+'%' : '-';
    const api = m.api_key_turns || 0;
    return '<tr>' +
      '<td>'+(m.date||'-')+'</td>' +
      '<td><strong>'+(m.score!=null?m.score:'-')+'</strong></td>' +
      '<td>'+t1r+'</td>' +
      '<td class="'+mc+'">'+ms+'</td>' +
      '<td>'+(m.sessions!=null?m.sessions:'-')+'</td>' +
      '<td>'+(m.resolved_sessions!=null?m.resolved_sessions:'-')+'</td>' +
      '<td class="'+(api>0?'crit':'')+'">'+api+'</td>' +
      '</tr>';
  }).join('');
}

function renderProposalHistory(botId, allProposals) {
  const container = document.getElementById('detail-proposal-history');
  const icons = {pending:'&#128309;',approved:'&#9989;',deployed:'&#128640;'};
  const all = [
    ...(allProposals.pending||[]).map(p=>({...p,_s:'pending'})),
    ...(allProposals.approved||[]).map(p=>({...p,_s:'approved'})),
    ...(allProposals.deployed||[]).map(p=>({...p,_s:'deployed'})),
  ].filter(p=>p.target_bot===botId)
   .sort((a,b)=>(b.generated||'').localeCompare(a.generated||''));

  if (!all.length) {
    container.innerHTML = '<div style="color:var(--text2);font-size:0.85rem">No proposals for this bot yet.</div>';
    return;
  }
  container.innerHTML = all.map(p => {
    const date = (p.generated||'').slice(0,10);
    return '<div class="ph-row">' +
      '<div style="font-size:1rem;flex-shrink:0">'+(icons[p._s]||'?')+'</div>' +
      '<div style="flex:1">' +
        '<div style="font-weight:500">'+(p.problem||'')+'</div>' +
        '<div style="color:var(--text2);font-size:0.78rem;margin-top:3px">'+(p.type||'')+' &middot; conf: '+((p.confidence||0)*100).toFixed(0)+'% &middot; '+p._s+'</div>' +
      '</div>' +
      '<div class="ph-date">'+date+'</div>' +
      '</div>';
  }).join('');
}"""

content = content.replace(OLD_FN, NEW_FN, 1)

SRC.write_text(content)
print("Dashboard patched successfully")
print(f"  File size: {len(content):,} bytes")
