/* Jimbot dashboard — client-side rendering.
 * Fetches docs/data/metrics.json and renders every chart + table.
 * Chart.js is loaded via CDN from the HTML file.
 */
(() => {
  const PALETTE = {
    accent:  '#7cc4ff',
    good:    '#7fd48a',
    bad:     '#ff7a7a',
    warn:    '#f5c26b',
    neutral: '#8b92a3',
    violet:  '#c084fc',
    teal:    '#5eead4',
    amber:   '#fbbf24',
    pink:    '#f472b6',
    blue:    '#60a5fa',
  };
  const SOURCE_COLORS = {
    preflop_gto:     PALETTE.blue,
    solver:          PALETTE.good,
    turn_heuristic:  PALETTE.amber,
    river_heuristic: PALETTE.violet,
    monte_carlo:     PALETTE.bad,
  };
  const ACTION_COLORS = {
    fold:  PALETTE.bad,
    check: PALETTE.neutral,
    call:  PALETTE.amber,
    raise: PALETTE.good,
  };

  // Chart.js global defaults to match dark theme
  Chart.defaults.color = '#8b92a3';
  Chart.defaults.borderColor = '#2a2f3a';
  Chart.defaults.font.family = 'ui-monospace, SFMono-Regular, Menlo, monospace';

  // ── helpers ────────────────────────────────────────────────────────
  const $  = (sel) => document.querySelector(sel);
  const fmt_bb  = (v) => (v >= 0 ? '+' : '') + v.toFixed(2);
  const fmt_pct = (v) => (v * 100).toFixed(0) + '%';
  const pos_neg = (v) => v > 0 ? 'good' : (v < 0 ? 'bad' : '');

  function card(label, value, sub, cls) {
    const tone = cls || pos_neg(typeof value === 'string' ? parseFloat(value) : value);
    return `<div class="card">
      <div class="label">${label}</div>
      <div class="value ${tone}">${value}</div>
      ${sub ? `<div class="sub">${sub}</div>` : ''}
    </div>`;
  }

  // ── boot ───────────────────────────────────────────────────────────
  fetch('data/metrics.json')
    .then(r => r.ok ? r.json() : Promise.reject(r.statusText))
    .then(render)
    .catch(err => {
      console.error('[dashboard] failed to load metrics.json', err);
      $('#empty-state').classList.remove('hidden');
    });

  function render(data) {
    const overall = data.overall || {};
    const sessions = data.sessions || [];
    const versions = data.versions || [];

    // Header meta
    $('#generated-at').textContent = data.generated_at || '—';
    const latest = versions.length ? versions[versions.length - 1].version : '—';
    $('#current-version').textContent = latest;

    if (!sessions.length) {
      $('#empty-state').classList.remove('hidden');
      return;
    }

    renderOverview(overall, sessions);
    renderDecisions(overall);
    renderVersions(versions);
    renderOpponents(overall);
    renderSessions(sessions);
  }

  // ── overview section ───────────────────────────────────────────────
  function renderOverview(overall, sessions) {
    const cards = [
      card('Total hands',      overall.hands, `${overall.sessions} sessions`, 'accent'),
      card('Total BB',         fmt_bb(overall.total_bb)),
      card('BB / 100',         fmt_bb(overall.bb_per_100)),
      card('Winning sessions', `${overall.winning_sessions}`, `of ${overall.sessions} (${fmt_pct(overall.winning_sessions / Math.max(1, overall.sessions))})`, ''),
      card('WTSD',             fmt_pct(overall.wtsd_avg), 'avg across sessions'),
    ];
    $('#overview-cards').innerHTML = cards.join('');

    // Cumulative P&L — concat all sessions' pnl_series in date order
    const cumHands = [];
    const cumBB    = [];
    let idx = 0;
    let running = 0;
    for (const s of sessions) {
      for (const p of (s.pnl_series || [])) {
        idx += 1;
        running += p.bb_delta;
        cumHands.push(idx);
        cumBB.push(running);
      }
    }
    new Chart($('#chart-pnl'), {
      type: 'line',
      data: {
        labels: cumHands,
        datasets: [{
          label: 'Cumulative BB',
          data: cumBB,
          borderColor: PALETTE.accent, backgroundColor: 'rgba(124,196,255,0.1)',
          fill: true, pointRadius: 0, tension: 0.15, borderWidth: 2,
        }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        scales: {
          x: { title: { display: true, text: 'Hand #' }, ticks: { maxTicksLimit: 10 } },
          y: { title: { display: true, text: 'BB' } },
        },
        plugins: { legend: { display: false } },
      },
    });

    // BB delta per session
    new Chart($('#chart-sessions-bb'), {
      type: 'bar',
      data: {
        labels: sessions.map(s => s.session_id.slice(5, 16)),   // shorten timestamp
        datasets: [{
          label: 'BB',
          data: sessions.map(s => s.total_bb),
          backgroundColor: sessions.map(s => s.total_bb >= 0 ? PALETTE.good : PALETTE.bad),
        }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        scales: { y: { title: { display: true, text: 'BB' } } },
        plugins: { legend: { display: false } },
      },
    });
  }

  // ── decisions section ──────────────────────────────────────────────
  function renderDecisions(overall) {
    const sources = overall.source_counts || {};
    const srcLabels = Object.keys(sources);
    new Chart($('#chart-source'), {
      type: 'doughnut',
      data: {
        labels: srcLabels,
        datasets: [{
          data: srcLabels.map(k => sources[k]),
          backgroundColor: srcLabels.map(k => SOURCE_COLORS[k] || PALETTE.neutral),
          borderColor: '#0f1115',
        }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { position: 'bottom' } },
      },
    });

    // Per-phase action stacks
    const phases = ['preflop', 'flop', 'turn', 'river'];
    const actions = ['fold', 'call', 'check', 'raise'];
    const paData = overall.phase_action || {};
    const datasets = actions.map(a => ({
      label: a,
      data: phases.map(p => (paData[p] || {})[a] || 0),
      backgroundColor: ACTION_COLORS[a],
      stack: 'stack',
    }));
    new Chart($('#chart-phase-action'), {
      type: 'bar',
      data: { labels: phases, datasets },
      options: {
        responsive: true, maintainAspectRatio: false,
        scales: { x: { stacked: true }, y: { stacked: true } },
        plugins: { legend: { position: 'bottom' } },
      },
    });

    // Hero stat cards
    const hero = [
      card('Hero VPIP', fmt_pct(overall.hero_vpip_avg), 'voluntary preflop %'),
      card('Hero PFR',  fmt_pct(overall.hero_pfr_avg),  'preflop raise %'),
      card('C-bet',     fmt_pct(overall.hero_cbet_avg), 'flop cbet when PFR'),
      card('Decisions', (overall.source_counts ? Object.values(overall.source_counts).reduce((a,b)=>a+b,0) : 0), 'total across sessions', 'accent'),
    ];
    $('#hero-stats-cards').innerHTML = hero.join('');
  }

  // ── versions section ───────────────────────────────────────────────
  function renderVersions(versions) {
    const body = versions.map(v => `
      <tr>
        <td>${v.version}</td>
        <td class="num">${v.sessions}</td>
        <td class="num">${v.hands}</td>
        <td class="num ${pos_neg(v.total_bb)}">${fmt_bb(v.total_bb)}</td>
        <td class="num ${pos_neg(v.bb_per_100)}">${fmt_bb(v.bb_per_100)}</td>
        <td class="num">${fmt_pct(v.winrate)}</td>
        <td class="num">${fmt_pct(v.wtsd)}</td>
        <td class="num">${fmt_pct(v.hero_vpip)}</td>
        <td class="num">${fmt_pct(v.hero_pfr)}</td>
        <td class="num">${fmt_pct(v.hero_cbet)}</td>
      </tr>`).join('');
    $('#versions-table tbody').innerHTML = body;

    new Chart($('#chart-version-bb'), {
      type: 'bar',
      data: {
        labels: versions.map(v => `v${v.version}`),
        datasets: [{
          label: 'BB / 100',
          data: versions.map(v => v.bb_per_100),
          backgroundColor: versions.map(v => v.bb_per_100 >= 0 ? PALETTE.good : PALETTE.bad),
        }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        scales: { y: { title: { display: true, text: 'BB / 100 hands' } } },
        plugins: { legend: { display: false } },
      },
    });
  }

  // ── opponents section ──────────────────────────────────────────────
  function renderOpponents(overall) {
    const opps = overall.top_opponents || [];
    $('#opponents-table tbody').innerHTML = opps.map(o => `
      <tr><td>${o.name}</td><td class="num">${o.hands}</td></tr>
    `).join('') || '<tr><td colspan="2" class="muted">no opponents recorded</td></tr>';
  }

  // ── sessions section ───────────────────────────────────────────────
  function renderSessions(sessions) {
    $('#sessions-table tbody').innerHTML = sessions.slice().reverse().map(s => `
      <tr>
        <td>${s.date}</td>
        <td>${s.session_id}</td>
        <td>${s.version}</td>
        <td class="num">${s.hands}</td>
        <td class="num ${pos_neg(s.total_bb)}">${fmt_bb(s.total_bb)}</td>
        <td class="num ${pos_neg(s.bb_per_100)}">${fmt_bb(s.bb_per_100)}</td>
        <td class="num">${fmt_pct(s.winrate)}</td>
        <td class="num">${fmt_pct(s.wtsd)}</td>
      </tr>
    `).join('');
  }
})();
