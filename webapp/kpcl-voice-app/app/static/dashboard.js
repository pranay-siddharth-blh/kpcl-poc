// dashboard.html: page script (served from a file so the security policy allows no inline scripts)
// ------------------------------------------------------------------ labels (the takeaways come from the server in English)
const I18N = {
  en: { govLine1: 'Government of Karnataka', govLine2: 'Karnataka Power Corporation Limited',
    appBar: '<b>Generation Management System</b><span class="extra"><span class="sep">|</span>Dashboard<span class="sep">|</span>April 2015 – April 2025</span>',
    aiBadge: 'AI MODE', menu: 'Menu', navDash: 'Dashboard', navChat: 'Ask the assistant', navInsights: 'Insights', goInsights: 'Opening Insights', allStations: 'All stations', askAi: 'Ask AI about this',
    takeaways: 'Insights', monthlyTitle: 'Monthly generation', weeklyTitle: 'Weekly generation', dailyTitle: 'Daily generation', grainMonthly: 'Monthly', grainWeekly: 'Weekly', grainDaily: 'Daily', gsBest: 'Best', gsLow: 'Weakest', gsAvg: 'Average', weekByWeek: 'Week by week', weekHint: 'Point at a week to find it on the chart; click a bar to jump to its month', mixTitle: 'Generation mix', thermalShare: 'thermal share', unitsTitle: 'Units',
    leagueTitle: 'Stations', leagueHint: 'Click a station for its own dashboard', outTitle: 'Units out of service', coalTitle: 'Coal stock',
    resTitle: 'Reservoir levels', lossTitle: 'Where generation was lost',
    footNote: 'Figures come from fixed, read-only queries on the Generation Management System data. Proof-of-concept data; only the RTPS and SGS figures pinned to K-eRP reports are real.',
    misReport: 'MIS report', misView: 'View', misFor: 'Excel MIS report for', misOnScreen: 'As on screen', misComplete: 'Complete report', misCompleteSub: 'Monthly, weekly and daily sheets together', misFoot: 'Figures match this dashboard. Includes summary, station or unit tables, trends, losses, coal, reservoirs and integrity checks.', misBusy: 'Preparing your MIS report…', misDone: 'MIS report downloaded', misFail: 'The MIS report could not be prepared. Please try again.',
    overview: 'KPCL at a glance', dashboard: 'Dashboard', stationDash: 'Station dashboard', groups: 'Stations' },
  kn: { govLine1: 'ಕರ್ನಾಟಕ ಸರ್ಕಾರ', govLine2: 'ಕರ್ನಾಟಕ ವಿದ್ಯುತ್ ನಿಗಮ ನಿಯಮಿತ',
    appBar: '<b>ಉತ್ಪಾದನಾ ನಿರ್ವಹಣಾ ವ್ಯವಸ್ಥೆ</b><span class="extra"><span class="sep">|</span>ಡ್ಯಾಶ್‌ಬೋರ್ಡ್<span class="sep">|</span>ಏಪ್ರಿಲ್ 2015 – ಏಪ್ರಿಲ್ 2025</span>',
    aiBadge: 'AI ಮೋಡ್', menu: 'ಮೆನು', navDash: 'ಡ್ಯಾಶ್‌ಬೋರ್ಡ್', navChat: 'ಸಹಾಯಕನನ್ನು ಕೇಳಿ', navInsights: 'ಒಳನೋಟಗಳು', goInsights: 'ಒಳನೋಟಗಳನ್ನು ತೆರೆಯಲಾಗುತ್ತಿದೆ', allStations: 'ಎಲ್ಲಾ ಕೇಂದ್ರಗಳು', askAi: 'AI ಅನ್ನು ಕೇಳಿ',
    takeaways: 'ಒಳನೋಟಗಳು', monthlyTitle: 'ಮಾಸಿಕ ಉತ್ಪಾದನೆ', weeklyTitle: 'ವಾರದ ಉತ್ಪಾದನೆ', dailyTitle: 'ದೈನಂದಿನ ಉತ್ಪಾದನೆ', grainMonthly: 'ಮಾಸಿಕ', grainWeekly: 'ವಾರ', grainDaily: 'ದೈನಂದಿನ', gsBest: 'ಗರಿಷ್ಠ', gsLow: 'ಕನಿಷ್ಠ', gsAvg: 'ಸರಾಸರಿ', weekByWeek: 'ವಾರವಾರು', weekHint: 'ಚಾರ್ಟ್‌ನಲ್ಲಿ ನೋಡಲು ವಾರದ ಮೇಲೆ ತೋರಿಸಿ', mixTitle: 'ಉತ್ಪಾದನಾ ಮಿಶ್ರಣ', thermalShare: 'ಉಷ್ಣ ಪಾಲು', unitsTitle: 'ಘಟಕಗಳು',
    leagueTitle: 'ಕೇಂದ್ರಗಳು', leagueHint: 'ಕೇಂದ್ರದ ಡ್ಯಾಶ್‌ಬೋರ್ಡ್‌ಗಾಗಿ ಕ್ಲಿಕ್ ಮಾಡಿ', outTitle: 'ಸ್ಥಗಿತಗೊಂಡ ಘಟಕಗಳು', coalTitle: 'ಕಲ್ಲಿದ್ದಲು ದಾಸ್ತಾನು',
    resTitle: 'ಜಲಾಶಯ ಮಟ್ಟಗಳು', lossTitle: 'ಉತ್ಪಾದನೆ ಎಲ್ಲಿ ನಷ್ಟವಾಯಿತು',
    footNote: 'ಅಂಕಿಅಂಶಗಳು ಸ್ಥಿರ, ಓದಲು-ಮಾತ್ರ ಪ್ರಶ್ನೆಗಳಿಂದ ಬರುತ್ತವೆ. ಪ್ರಾಯೋಗಿಕ ದತ್ತಾಂಶ; K-eRP ವರದಿಗಳಿಗೆ ಹೊಂದಿಸಿದ RTPS ಮತ್ತು SGS ಅಂಕಿಅಂಶಗಳು ಮಾತ್ರ ನೈಜ.',
    misReport: 'MIS ವರದಿ', misView: 'ವೀಕ್ಷಿಸಿ', misFor: 'Excel MIS ವರದಿ', misOnScreen: 'ಪರದೆಯಲ್ಲಿ ಕಾಣುವಂತೆ', misComplete: 'ಪೂರ್ಣ ವರದಿ', misCompleteSub: 'ಮಾಸಿಕ, ವಾರ ಮತ್ತು ದೈನಂದಿನ ಹಾಳೆಗಳು ಒಟ್ಟಿಗೆ', misFoot: 'ಅಂಕಿಅಂಶಗಳು ಈ ಡ್ಯಾಶ್‌ಬೋರ್ಡ್‌ಗೆ ಹೊಂದುತ್ತವೆ. ವರದಿ ಇಂಗ್ಲಿಷ್‌ನಲ್ಲಿದೆ.', misBusy: 'ವರದಿ ಸಿದ್ಧಪಡಿಸಲಾಗುತ್ತಿದೆ…', misDone: 'MIS ವರದಿ ಡೌನ್‌ಲೋಡ್ ಆಗಿದೆ', misFail: 'ವರದಿ ಸಿದ್ಧಪಡಿಸಲಾಗಲಿಲ್ಲ. ದಯವಿಟ್ಟು ಮತ್ತೆ ಪ್ರಯತ್ನಿಸಿ.',
    overview: 'ಕ.ವಿ.ನಿ.ನಿ ಒಂದು ನೋಟದಲ್ಲಿ', dashboard: 'ಡ್ಯಾಶ್‌ಬೋರ್ಡ್', stationDash: 'ಕೇಂದ್ರ ಡ್ಯಾಶ್‌ಬೋರ್ಡ್', groups: 'ಕೇಂದ್ರಗಳು' },
};
let lang = 'en';
const t = k => I18N[lang][k] ?? I18N.en[k];

const CAT = {
  'THERMAL-COAL': { label: 'Thermal (coal)', color: '#072577' }, 'MAJOR HYDRO': { label: 'Major hydro', color: '#2f8fd6' },
  'MINOR HYDRO': { label: 'Minor hydro', color: '#8cc1ea' }, 'GAS': { label: 'Gas', color: '#8e6819' },
  'SOLAR': { label: 'Solar', color: '#e9b949' }, 'WIND': { label: 'Wind', color: '#2a9d8f' }, 'RES': { label: 'Waste to energy', color: '#7a8699' },
};
const catOf = c => CAT[c] || { label: c, color: '#7a8699' };

// ------------------------------------------------------------------ helpers
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const md = s => esc(s).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>');
const num = (v, d = 1) => v == null ? '—' : Number(v).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
const pretty = d => { if (!d) return '—'; const [y, m, dd] = d.slice(0, 10).split('-'); return `${dd}-${'JanFebMarAprMayJunJulAugSepOctNovDec'.slice((m - 1) * 3, m * 3)}-${y}`; };
const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
function countUp(el, to, d = 1, ms = 1100){
  if (to == null){ el.textContent = '—'; return; }
  if (reduced){ el.textContent = num(to, d); return; }
  const t0 = performance.now();
  const step = now => { const p = Math.min(1, (now - t0) / ms), e = 1 - Math.pow(1 - p, 3);
    el.textContent = num(to * e, d); if (p < 1) requestAnimationFrame(step); };
  requestAnimationFrame(step);
}
function delta(cur, prev, { unit = '%', pts = false, goodUp = true, d = 1 } = {}){
  if (cur == null || prev == null) return '';
  const diff = pts ? cur - prev : (prev ? (cur - prev) / prev * 100 : null);
  if (diff == null) return '';
  const up = diff >= 0, good = goodUp ? up : !up, cls = Math.abs(diff) < 0.05 ? 'flat' : good ? 'up' : 'down';
  return `<span class="chip ${cls}">${up ? '▲' : '▼'} ${num(Math.abs(diff), d)}${pts ? ' pts' : unit}</span>`;
}
const achClass = a => a == null ? '' : a >= 100 ? 'pct-good' : a >= 95 ? 'pct-warn' : 'pct-bad';
const achColor = a => a == null ? '#cfd5e2' : a >= 100 ? '#1f9d57' : a >= 95 ? '#c98a00' : '#c8372d';

const charts = {};
function chart(id, cfg){
  if (!window.Chart) return null;
  if (charts[id]) charts[id].destroy();
  charts[id] = new Chart($(id).getContext('2d'), cfg);
  return charts[id];
}
const FONT = { family: 'Noto Sans', size: 11 };
function spark(id, values, color, fillTop){
  const cv = $(id); if (!cv || !window.Chart) return;
  const ctx = cv.getContext('2d'); const g = ctx.createLinearGradient(0, 0, 0, cv.clientHeight || 60);
  g.addColorStop(0, fillTop); g.addColorStop(1, 'rgba(255,255,255,0)');
  chart(id, { type: 'line', data: { labels: values.map((_, i) => i), datasets: [{ data: values, borderColor: color, backgroundColor: g, fill: true,
    borderWidth: 2, tension: .35, pointRadius: values.map((_, i) => i === values.length - 1 ? 3 : 0), pointBackgroundColor: color }] },
    options: { responsive: true, maintainAspectRatio: false, animation: { duration: reduced ? 0 : 900 }, plugins: { legend: { display: false }, tooltip: { enabled: false } },
      scales: { x: { display: false }, y: { display: false } } } });
}

// ------------------------------------------------------------------ state
let DATA = null, FY = null, STATION = null, FILTER = 'ALL', SORT = { key: 'gen', dir: -1 };

async function load(pushState = true){
  $('error').hidden = true;
  const qs = new URLSearchParams(); if (FY) qs.set('fy', FY); if (STATION) qs.set('station', STATION);
  try {
    const r = await fetch('/api/dashboard?' + qs.toString());
    if (r.status === 404){ const b = await r.json().catch(() => ({})); throw new Error(b.detail || 'Not found'); }
    if (!r.ok) throw new Error('HTTP ' + r.status);
    DATA = await r.json(); FY = DATA.fy;
    if (pushState){ const u = new URL(location); u.search = ''; if (FY !== DATA.complete_fys.at(-1)) u.searchParams.set('fy', FY);
      if (STATION) u.searchParams.set('station', STATION); history.pushState({ fy: FY, station: STATION }, '', u); }
    render();
    $('skeleton').hidden = true; $('body').hidden = false;
  } catch (e) {
    if (window.console) console.error(e);
    $('skeleton').hidden = true;
    $('error').textContent = 'The dashboard could not be loaded. ' + (e.message || ''); $('error').hidden = false;
    if (STATION){ STATION = null; }
  }
}

// ------------------------------------------------------------------ render
function render(){
  if (!$('stationNav').children.length) renderStationNav();
  const d = DATA, k = d.kpis, cur = k.cur, prev = k.prev, st = d.scope;
  // heading
  $('eyebrow').textContent = st ? t('stationDash') + ' · ' + catOf(st.category).label : t('dashboard');
  $('title').textContent = st ? st.name : t('overview');
  $('subline').innerHTML = `FY <b>${esc(d.fy)}</b>${d.is_partial ? ' (incomplete year)' : ''}${d.prev_fy ? ' · compared with FY ' + esc(d.prev_fy) : ''} · data to <b>${pretty(d.as_of)}</b>`
    + (st ? ` · ${num(st.capacity_mw, 1)} MW · ${st.unit_count} units` : ` · ${k.station_count} stations · ${num(k.installed_mw, 0)} MW`);
  $('backBtn').hidden = !st;
  const sel = $('fySelect');
  sel.innerHTML = d.fys.slice().reverse().map(f => `<option value="${esc(f)}" ${f === d.fy ? 'selected' : ''}>FY ${esc(f)}${d.complete_fys.includes(f) ? '' : ' (part)'}</option>`).join('');
  document.querySelectorAll('.st-item').forEach(b => b.classList.toggle('active', b.dataset.st === (st && st.station)));
  $('navOverview').classList.toggle('active', !st);

  // hero
  $('bigLabel').textContent = `Gross generation · FY ${d.fy}`;
  countUp($('bigVal'), cur.gen, 1, 1300);
  $('bigRow').innerHTML = (d.is_partial ? '' : delta(cur.gen, prev.gen, { d: 1 })) + (d.prev_fy ? `<span>FY ${esc(d.prev_fy)}: ${num(prev.gen, 1)} MU</span>` : '');
  spark('bigSpark', d.series.gen.map(x => x.v), '#e9b949', 'rgba(233,185,73,.35)');

  renderKpis();
  renderMonthly();
  $('mixPanel').hidden = !!st; $('unitsPanel').hidden = !st; $('leaguePanel').hidden = !!st; $('topGrid').classList.toggle('station', !!st);
  if (st) renderUnits(); else { renderMix(); renderLeague(); }
  renderOut(); renderCoal(); renderReservoirs(); renderLosses(); renderTrend();
  ['asof1', 'asof2', 'asof3'].forEach(id => $(id).textContent = 'As on ' + pretty(d.as_of));
}

function kpiCard({ title, value, d = 1, unit = '', foot = '', accent = '#072577', soft = '#eef1f8', icon, spark: sp, alert = false, ring = null, i = 0 }){
  const id = 'sp' + Math.random().toString(36).slice(2, 8);
  const ringSvg = ring != null ? (() => { const r = 26, c = 2 * Math.PI * r, p = Math.max(0, Math.min(1, ring / 100));
    return `<svg viewBox="0 0 64 64"><circle cx="32" cy="32" r="${r}" fill="none" stroke="#eef1f8" stroke-width="7"/>
      <circle cx="32" cy="32" r="${r}" fill="none" stroke="${accent}" stroke-width="7" stroke-linecap="round" stroke-dasharray="${c}" stroke-dashoffset="${c}"
      style="transition:stroke-dashoffset 1.2s cubic-bezier(.2,.8,.2,1)" data-to="${c * (1 - p)}"/></svg>`; })() : '';
  const v = `<div class="v"><span data-count="${value ?? ''}" data-d="${d}">—</span><small>${esc(unit)}</small></div>`;
  return `<div class="kpi${alert ? ' alert' : ''}" style="--accent:${accent};--accent-soft:${soft};animation-delay:${i * 60}ms">
    <div class="top"><span class="ico"><svg viewBox="0 0 24 24">${icon}</svg></span><span class="t">${esc(title)}</span></div>
    ${ring != null ? `<div class="ring">${ringSvg}${v}</div>` : v}
    <div class="foot">${foot}</div>${sp ? `<div class="spark"><canvas id="${id}" data-spark='${JSON.stringify(sp)}'></canvas></div>` : ''}</div>`;
}
const IC = {
  gauge: '<path d="M12 14l4-4"/><path d="M3.3 17a9 9 0 1 1 17.4 0"/><circle cx="12" cy="14" r="1.2"/>',
  target: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
  drop: '<path d="M12 2.7C9 7 6 10.3 6 14a6 6 0 0 0 12 0c0-3.7-3-7-6-11.3z"/>',
  power: '<path d="M18.4 6.6a9 9 0 1 1-12.8 0"/><path d="M12 2v10"/>',
  grid: '<path d="M4 20L12 3l8 17"/><path d="M7 13h10M6 17h12"/>',
  plug: '<path d="M9 2v6M15 2v6M6 8h12v4a6 6 0 0 1-12 0z"/><path d="M12 18v4"/>',
  bolt: '<path d="M13 2L4 14h7l-1 8 9-12h-7z"/>',
};
function renderKpis(){
  const d = DATA, k = d.kpis, cur = k.cur, prev = k.prev, st = d.scope, s = d.series;
  const cards = [];
  if (st){
    cards.push({ title: 'Plant load factor', value: cur.plf, d: 2, unit: '%', icon: IC.gauge, foot: delta(cur.plf, prev.plf, { pts: true, d: 2 }) + (d.prev_fy ? `FY ${d.prev_fy}: ${num(prev.plf, 2)}%` : ''), spark: s.plf.map(x => x.v) });
    cards.push({ title: 'CEA target achievement', value: cur.achievement, d: 1, unit: '%', icon: IC.target, ring: cur.achievement, accent: achColor(cur.achievement), soft: '#f4f6fa',
      foot: `Target ${num(cur.target, 1)} MU` });
    cards.push({ title: 'Auxiliary consumption', value: cur.aux, d: 2, unit: '%', icon: IC.plug, accent: '#8e6819', soft: '#f7f1e3', foot: delta(cur.aux, prev.aux, { pts: true, goodUp: false, d: 2 }) + 'lower is better', spark: s.aux.map(x => x.v) });
    cards.push({ title: 'Units out of service', value: k.units_out, d: 0, unit: `of ${k.units_total}`, icon: IC.power, alert: k.units_out > 0, accent: k.units_out ? '#c8372d' : '#1f9d57', soft: k.units_out ? '#fcebea' : '#e5f5ec',
      foot: k.units_out ? `${num(k.mw_out, 0)} MW unavailable · as on ${pretty(d.as_of)}` : `as on ${pretty(d.as_of)}` });
    cards.push({ title: 'Trips in the year', value: cur.trips, d: 0, unit: '', icon: IC.bolt, accent: '#2f8fd6', soft: '#e6f2fb', foot: delta(cur.trips, prev.trips, { goodUp: false, d: 0 }) + (d.prev_fy ? `FY ${d.prev_fy}: ${num(prev.trips, 0)}` : '') });
  } else {
    cards.push({ title: 'Thermal PLF', value: cur.thermal_plf, d: 2, unit: '%', icon: IC.gauge, foot: delta(cur.thermal_plf, prev.thermal_plf, { pts: true, d: 2 }) + (d.prev_fy ? `FY ${d.prev_fy}: ${num(prev.thermal_plf, 2)}%` : ''), spark: s.thermal_plf.map(x => x.v) });
    cards.push({ title: 'CEA target achievement', value: cur.achievement, d: 1, unit: '%', icon: IC.target, ring: cur.achievement, accent: achColor(cur.achievement), soft: '#f4f6fa',
      foot: `${num(cur.gen, 0)} of ${num(cur.target, 0)} MU` });
    cards.push({ title: 'Hydro generation', value: cur.hydro_gen, d: 1, unit: 'MU', icon: IC.drop, accent: '#2f8fd6', soft: '#e6f2fb', foot: delta(cur.hydro_gen, prev.hydro_gen, { d: 1 }) + (d.prev_fy ? `FY ${d.prev_fy}: ${num(prev.hydro_gen, 0)} MU` : ''), spark: s.hydro_gen.map(x => x.v) });
    cards.push({ title: 'Units out of service', value: k.units_out, d: 0, unit: `of ${k.units_total}`, icon: IC.power, alert: k.units_out > 0, accent: k.units_out ? '#c8372d' : '#1f9d57', soft: k.units_out ? '#fcebea' : '#e5f5ec',
      foot: k.units_out ? `${num(k.mw_out, 0)} MW unavailable · as on ${pretty(d.as_of)}` : `as on ${pretty(d.as_of)}` });
    cards.push({ title: "Share of Karnataka's consumption", value: k.share, d: 1, unit: '%', icon: IC.grid, accent: '#b8892b', soft: '#f7f1e3', foot: delta(k.share, k.share_prev, { pts: true, d: 1 }) + (k.peak_mw ? `state peak ${num(k.peak_mw, 0)} MW` : '') });
  }
  $('kpis').innerHTML = cards.map((c, i) => kpiCard({ ...c, i })).join('');
  document.querySelectorAll('#kpis [data-count]').forEach(el => { const v = el.dataset.count; if (v === '') el.textContent = '—'; else countUp(el, Number(v), Number(el.dataset.d)); });
  requestAnimationFrame(() => document.querySelectorAll('#kpis circle[data-to]').forEach(c => c.style.strokeDashoffset = c.dataset.to));
  document.querySelectorAll('#kpis canvas[data-spark]').forEach(cv => {
    const acc = getComputedStyle(cv.closest('.kpi')).getPropertyValue('--accent').trim() || '#072577';
    spark(cv.id, JSON.parse(cv.dataset.spark), acc, acc + '33');
  });
}

// Generation panel: Monthly (with the CEA target), Weekly (Monday-Sunday calendar weeks) or Daily, for the same scope
// (all stations, or the station that is open) and FY. The choice is kept when the FY or the station changes.
let GRAIN = (() => { try { return localStorage.getItem('kpcl-grain') || 'monthly'; } catch (_e) { return 'monthly'; } })();
const MON3 = 'JanFebMarAprMayJunJulAugSepOctNovDec';
const mon = m => MON3.slice((Number(m) - 1) * 3, Number(m) * 3);
const dmy = d => d ? `${d.slice(8, 10)}-${mon(d.slice(5, 7))}-${d.slice(0, 4)}` : '—';
const dm = d => d ? `${d.slice(8, 10)} ${mon(d.slice(5, 7))}` : '';
const monthName = x => { const m = x.month || x.label || ''; return m.charAt(0) + m.slice(1, 3).toLowerCase() + (m.length > 4 ? ' ' + m.slice(-4) : ''); };
function setGrain(g){
  GRAIN = g;
  try { localStorage.setItem('kpcl-grain', g); } catch (_e) {}
  renderMonthly();
}
function renderMonthly(){
  const d = DATA, g = GRAIN;
  const tog = $('grainToggle'), btns = [...tog.querySelectorAll('button')];
  btns.forEach((b, i) => { const on = b.dataset.g === g; b.setAttribute('aria-selected', on); b.classList.toggle('on', on); if (on) tog.style.setProperty('--i', i); });
  btns.forEach(b => b.onclick = () => { if (b.dataset.g !== GRAIN) setGrain(b.dataset.g); });
  $('genTitle').textContent = t(g + 'Title');
  const prevTxt = !d.prev_fy ? '' : g === 'monthly' ? ` against FY ${d.prev_fy}` : g === 'weekly' ? ' against the same weeks a year earlier'
    : ' against the same dates a year earlier';
  updateMis();
  $('monthlyHint').textContent = `FY ${d.fy}${prevTxt}` + (g === 'monthly' ? ' and the CEA target' : '');
  const rows = g === 'monthly' ? d.monthly : g === 'weekly' ? d.weekly : d.daily;
  const hasPrev = !!d.prev_fy && rows.some(x => x.prev != null);
  $('monthlyLegend').innerHTML = `<span><i style="background:#16357f"></i>FY ${esc(d.fy)}</span>`
    + (hasPrev ? `<span><i style="background:#e9b949"></i>FY ${esc(d.prev_fy)}</span>` : '')
    + (g === 'monthly' ? `<span><i style="background:#c8372d;height:2px"></i>CEA target</span>` : `<span class="lg-note">CEA targets are set monthly</span>`);
  const empty = !rows.length || rows.every(x => !x.gen);
  $('genEmpty').hidden = !empty;
  $('genEmpty').textContent = `No generation is recorded ${d.scope ? 'for ' + d.scope.station + ' ' : ''}in FY ${d.fy}.`;
  $('grainStats').innerHTML = empty ? '' : grainStats(g, rows);
  if (!window.Chart){ renderWeekRibbon(g === 'weekly' && !empty ? rows : null, null); return; }
  if (empty){ if (charts.monthlyChart){ charts.monthlyChart.destroy(); delete charts.monthlyChart; } renderWeekRibbon(null); return; }
  const ctx = $('monthlyChart').getContext('2d');
  const grad = ctx.createLinearGradient(0, 0, 0, 280); grad.addColorStop(0, '#2d55c6'); grad.addColorStop(1, '#072577');
  const area = ctx.createLinearGradient(0, 0, 0, 280); area.addColorStop(0, 'rgba(22,53,127,.30)'); area.addColorStop(1, 'rgba(22,53,127,0)');
  const labels = g === 'monthly' ? rows.map(x => x.label) : g === 'weekly' ? rows.map(x => 'W' + x.week) : rows.map(x => dm(x.date));
  const cur = g === 'daily'
    ? { type: 'line', label: 'FY ' + d.fy, data: rows.map(x => x.gen), borderColor: '#16357f', backgroundColor: area, fill: true, borderWidth: 1.8,
        pointRadius: 0, pointHoverRadius: 4, tension: .25, order: 2 }
    : { type: 'bar', label: 'FY ' + d.fy, data: rows.map(x => x.gen), backgroundColor: grad, borderRadius: g === 'weekly' ? 3 : 6,
        maxBarThickness: g === 'weekly' ? 14 : 34, order: 3 };
  const prev = hasPrev ? [{ type: 'line', label: 'FY ' + d.prev_fy, data: rows.map(x => x.prev), borderColor: '#e9b949', backgroundColor: '#e9b949',
    borderWidth: g === 'daily' ? 1.4 : 2.2, borderDash: g === 'daily' ? [4, 3] : [], pointRadius: g === 'monthly' ? 3 : 0, pointHoverRadius: 4,
    tension: .35, order: 1 }] : [];
  const tgt = g === 'monthly' ? [{ type: 'line', label: 'CEA target', data: rows.map(x => x.target), borderColor: '#c8372d', borderDash: [6, 4],
    borderWidth: 2, pointRadius: 0, tension: .2, order: 2 }] : [];
  const title = items => {
    const x = rows[items[0].dataIndex];
    if (g === 'weekly') return `Week ${x.week} · ${dmy(x.first)} to ${dmy(x.last)}${x.days < 7 ? ` (${x.days} days)` : ''}`;
    if (g === 'daily') return `${x.day}, ${dmy(x.date)}`;
    return monthName(x);
  };
  const after = items => { const x = rows[items[0].dataIndex]; return x.plf != null ? `PLF ${num(x.plf, 2)}%` : ''; };
  // weekly and daily: the weeks / days of each month sit in a shaded band with the month named in a ribbon on top
  const bands = g === 'monthly' ? [] : monthGroups(rows.map(x => g === 'weekly' ? x.month : ymToLabel(x.date)));
  const c = chart('monthlyChart', { data: { labels, datasets: [cur, ...prev, ...tgt] }, plugins: [MONTH_BANDS],
    options: { responsive: true, maintainAspectRatio: false, animation: { duration: reduced ? 0 : 700 }, interaction: { mode: 'index', intersect: false },
      layout: { padding: { top: bands.length ? 30 : 0 } },
      onHover: (e, els) => g === 'weekly' && markWeek(els.length ? els[0].index : null),
      onClick: (e, els) => { if (g === 'weekly' && els.length) focusWeek(els[0].index, true); },
      plugins: { legend: { display: false }, monthBands: { groups: bands },
        tooltip: { backgroundColor: '#04164a', padding: 10, cornerRadius: 8,
          callbacks: { title, afterBody: after, label: x => ` ${x.dataset.label}: ${num(x.raw, g === 'daily' ? 2 : 1)} MU` } } },
      scales: { x: { grid: { display: false }, ticks: { color: '#5f6b7d', font: FONT, autoSkip: true, maxRotation: 0,
                                                         maxTicksLimit: g === 'weekly' ? 18 : 12 } },
                y: { beginAtZero: true, grid: { color: 'rgba(7,37,119,.07)' }, border: { display: false },
                     ticks: { color: '#5f6b7d', font: FONT, callback: v => num(v, 0) }, title: { display: true, text: 'MU', color: '#5f6b7d', font: FONT } } } } });
  renderWeekRibbon(g === 'weekly' ? rows : null, c);
}

// ---- month bands (Chart.js plugin): alternate shading behind each month's bars and a month ribbon along the top
const MONTH_BANDS = {
  id: 'monthBands',
  beforeDatasetsDraw(ch, _a, opts){
    const groups = (opts && opts.groups) || [];
    if (!groups.length) return;
    const { ctx, chartArea: { top, bottom }, scales: { x } } = ch;
    const half = (x.getPixelForValue(1) - x.getPixelForValue(0)) / 2 || 4;
    ctx.save();
    groups.forEach((gr, i) => {
      const x0 = x.getPixelForValue(gr.start) - half, x1 = x.getPixelForValue(gr.end) + half;
      ctx.fillStyle = i % 2 ? 'rgba(184,137,43,.06)' : 'rgba(7,37,119,.035)';
      ctx.fillRect(x0, top, x1 - x0, bottom - top);
      if (i){ ctx.strokeStyle = 'rgba(7,37,119,.14)'; ctx.setLineDash([3, 3]); ctx.beginPath(); ctx.moveTo(x0, top); ctx.lineTo(x0, bottom); ctx.stroke(); ctx.setLineDash([]); }
    });
    ctx.restore();
  },
  afterDatasetsDraw(ch, _a, opts){
    const groups = (opts && opts.groups) || [];
    if (!groups.length) return;
    const { ctx, chartArea: { top }, scales: { x } } = ch;
    const half = (x.getPixelForValue(1) - x.getPixelForValue(0)) / 2 || 4;
    ctx.save();
    ctx.font = '700 10.5px "Noto Sans", sans-serif'; ctx.textBaseline = 'middle'; ctx.textAlign = 'center';
    groups.forEach((gr, i) => {
      const x0 = x.getPixelForValue(gr.start) - half + 2, x1 = x.getPixelForValue(gr.end) + half - 2, w = x1 - x0;
      if (w < 12) return;
      const full = gr.label, short = gr.label.slice(0, 3), txt = ctx.measureText(full).width + 14 <= w ? full : ctx.measureText(short).width + 10 <= w ? short : short[0];
      const y = top - 17, h = 18;
      ctx.fillStyle = i % 2 ? '#f7f1e3' : '#eef1f8';
      ctx.strokeStyle = i % 2 ? '#e4d3ab' : '#cfd5e2';
      ctx.beginPath(); ctx.roundRect ? ctx.roundRect(x0, y - h / 2, w, h, 9) : ctx.rect(x0, y - h / 2, w, h); ctx.fill(); ctx.stroke();
      ctx.fillStyle = i % 2 ? '#8e6819' : '#072577';
      ctx.fillText(txt, x0 + w / 2, y + .5);
    });
    ctx.restore();
  },
};
const MONTH_NAMES = { JAN: 'Jan', FEB: 'Feb', MAR: 'Mar', APR: 'Apr', MAY: 'May', JUN: 'Jun', JUL: 'Jul', AUG: 'Aug', SEP: 'Sep', OCT: 'Oct', NOV: 'Nov', DEC: 'Dec' };
const ymToLabel = date => `${MON3.slice((date.slice(5, 7) - 1) * 3, date.slice(5, 7) * 3).toUpperCase()}-${date.slice(0, 4)}`;
const niceMonth = key => `${MONTH_NAMES[key.slice(0, 3)] || key.slice(0, 3)} ${key.slice(4)}`;
function monthGroups(keys){
  const out = [];
  keys.forEach((k, i) => { if (out.length && out[out.length - 1].key === k) out[out.length - 1].end = i; else out.push({ key: k, label: niceMonth(k), start: i, end: i }); });
  return out;
}

// ---- week by week: one card per month (its exact calendar-month total) listing every week with its change on last year
function renderWeekRibbon(rows, ch){
  const host = $('weekRibbon');
  host.hidden = !rows;
  if (!rows) { host.innerHTML = ''; return; }
  const months = monthGroups(rows.map(x => x.month));
  const monthTotal = Object.fromEntries((DATA.monthly || []).map(m => [m.month, m.gen]));
  const chg = x => {
    if (x.prev == null || !x.prev) return '<span class="chg">—</span>';
    const p = (x.gen - x.prev) / x.prev * 100, up = p >= 0;
    const note = x.prev_days && x.prev_days !== x.days ? ' title="compared weeks have different lengths"' : '';
    return `<span class="chg ${up ? 'up' : 'down'}"${note}>${up ? '▲' : '▼'} ${num(Math.abs(p), 1)}%</span>`;
  };
  const day = d => Number(d.slice(8, 10));
  const range = x => { const a = x.first, b = x.last;
    return a.slice(5, 7) === b.slice(5, 7) ? `${day(a)}–${day(b)} ${mon(a.slice(5, 7))}`
      : `${day(a)} ${mon(a.slice(5, 7))}–${day(b)} ${mon(b.slice(5, 7))}`; };
  const maxGen = Math.max(...rows.map(x => x.gen || 0), 1);
  host.innerHTML = `<div class="wr-head"><h4>${esc(t('weekByWeek'))}</h4><span class="wr-hint">${esc(t('weekHint'))}</span>
      <div class="wr-nav"><button type="button" data-d="-1" aria-label="Earlier months">‹</button><button type="button" data-d="1" aria-label="Later months">›</button></div></div>
    <div class="wr-track" id="wrTrack">` + months.map((m, mi) => `<section class="wr-month ${mi % 2 ? 'alt' : ''}" data-m="${mi}">
      <header><b>${esc(m.label)}</b><span>${monthTotal[m.key] != null ? num(monthTotal[m.key], 1) + ' MU' : ''}</span></header>
      ${rows.slice(m.start, m.end + 1).map((x, k) => { const i = m.start + k;
        return `<button type="button" class="wr-week" data-i="${i}">
          <span class="wk">W${x.week}</span><span class="dt" title="${esc(dmy(x.first) + ' to ' + dmy(x.last))}">${esc(range(x))}${x.days < 7 ? ` <em>${x.days}d</em>` : ''}</span>
          <span class="mu">${num(x.gen, 1)}</span>${chg(x)}
          <span class="wr-bar"><i style="width:${(x.gen || 0) / maxGen * 100}%"></i></span></button>`; }).join('')}
    </section>`).join('') + '</div>';
  const track = $('wrTrack');
  host.querySelectorAll('.wr-nav button').forEach(b => b.onclick = () => track.scrollBy({ left: Number(b.dataset.d) * track.clientWidth * .8, behavior: reduced ? 'auto' : 'smooth' }));
  host.querySelectorAll('.wr-week').forEach(b => {
    const i = Number(b.dataset.i);
    b.onmouseenter = b.onfocus = () => { if (!ch) return; const els = ch.data.datasets.map((_, di) => ({ datasetIndex: di, index: i }));
      ch.setActiveElements(els); ch.tooltip.setActiveElements(els, { x: 0, y: 0 }); ch.update('none'); markWeek(i); };
    b.onmouseleave = b.onblur = () => { if (!ch) return; ch.setActiveElements([]); ch.tooltip.setActiveElements([], { x: 0, y: 0 }); ch.update('none'); markWeek(null); };
  });
}
function markWeek(i){
  document.querySelectorAll('#weekRibbon .wr-week.hl').forEach(b => b.classList.remove('hl'));
  if (i == null) return;
  const b = document.querySelector(`#weekRibbon .wr-week[data-i="${i}"]`);
  if (b) b.classList.add('hl');
}
function focusWeek(i, scroll){
  markWeek(i);
  const b = document.querySelector(`#weekRibbon .wr-week[data-i="${i}"]`);
  if (b && scroll){ const m = b.closest('.wr-month'), tr = $('wrTrack'); tr.scrollTo({ left: m.offsetLeft - tr.offsetLeft - 8, behavior: reduced ? 'auto' : 'smooth' }); }
}
function grainStats(g, rows){
  const full = g === 'weekly' ? rows.filter(x => x.days === 7) : rows;
  const pool = (full.length ? full : rows).filter(x => x.gen != null);
  if (!pool.length) return '';
  const best = pool.reduce((a, b) => (b.gen > a.gen ? b : a)), low = pool.reduce((a, b) => (b.gen < a.gen ? b : a));
  const avg = pool.reduce((a, b) => a + b.gen, 0) / pool.length;
  const name = x => g === 'weekly' ? `W${x.week} (${dm(x.first)})` : g === 'daily' ? dmy(x.date) : monthName(x);
  const unit = g === 'weekly' ? '/week' : g === 'daily' ? '/day' : '/month';
  return `<span class="gs up">▲ ${esc(t('gsBest'))} <b>${esc(name(best))}</b> ${num(best.gen, 1)} MU</span>`
    + `<span class="gs down">▼ ${esc(t('gsLow'))} <b>${esc(name(low))}</b> ${num(low.gen, 1)} MU</span>`
    + `<span class="gs">${esc(t('gsAvg'))} <b>${num(avg, 1)} MU${unit}</b></span>`;
}

function renderMix(){
  const d = DATA, mix = d.mix, total = mix.reduce((a, b) => a + (b.gen || 0), 0);
  $('mixHint').textContent = 'FY ' + d.fy;
  const thermal = mix.filter(x => x.category === 'THERMAL-COAL').reduce((a, b) => a + b.gen, 0);
  $('mixCenter').textContent = num(total ? thermal / total * 100 : null, 0) + '%';
  $('mixList').innerHTML = mix.map(x => `<div class="mix-row"><i style="background:${catOf(x.category).color}"></i><span class="nm">${esc(catOf(x.category).label)}</span>
    <span class="pc">${num(total ? x.gen / total * 100 : null, 1)}%</span><span class="mu">${num(x.gen, 0)} MU</span></div>`).join('');
  chart('mixChart', { type: 'doughnut', data: { labels: mix.map(x => catOf(x.category).label), datasets: [{ data: mix.map(x => x.gen), backgroundColor: mix.map(x => catOf(x.category).color), borderColor: '#fff', borderWidth: 3, hoverOffset: 8 }] },
    options: { cutout: '68%', responsive: true, maintainAspectRatio: false, animation: { duration: reduced ? 0 : 900 },
      plugins: { legend: { display: false }, tooltip: { backgroundColor: '#04164a', callbacks: { label: x => ` ${x.label}: ${num(x.raw, 1)} MU (${num(x.raw / total * 100, 1)}%)` } } } } });
}

function renderLeague(){
  const cats = [...new Set(DATA.stations.map(s => s.category))];
  $('filters').innerHTML = ['ALL', ...cats].map(c => `<button type="button" data-c="${esc(c)}" aria-pressed="${c === FILTER}">${c === 'ALL' ? 'All' : esc(catOf(c).label)}</button>`).join('');
  $('filters').querySelectorAll('button').forEach(b => b.onclick = () => { FILTER = b.dataset.c; renderLeague(); });
  const cols = [['name', 'Station', false], ['capacity_mw', 'MW', true], ['gen', 'Generation (MU)', true], ['yoy', 'vs last year', true], ['plf', 'PLF', true], ['achievement', 'CEA target', true]];
  let rows = DATA.stations.filter(s => FILTER === 'ALL' || s.category === FILTER);
  rows = rows.slice().sort((a, b) => { const x = a[SORT.key], y = b[SORT.key];
    if (x == null) return 1; if (y == null) return -1; return (typeof x === 'string' ? x.localeCompare(y) : x - y) * SORT.dir; });
  const maxGen = Math.max(...rows.map(s => s.gen || 0), 1);
  $('league').innerHTML = `<thead><tr>${cols.map(([k, l, n]) => `<th class="${n ? 'num' : ''}" data-k="${k}">${l}${SORT.key === k ? `<span class="arr">${SORT.dir < 0 ? '▼' : '▲'}</span>` : ''}</th>`).join('')}</tr></thead><tbody>`
    + rows.map(s => `<tr data-st="${esc(s.station)}">
      <td><div class="stn"><i style="background:${catOf(s.category).color}"></i><div><b>${esc(s.station)} ${s.units_out ? `<span class="badge-out">● ${s.units_out} out</span>` : ''}</b><span class="nm">${esc(s.name)}</span></div></div></td>
      <td class="num">${num(s.capacity_mw, 1)}</td>
      <td class="num"><span class="bar" style="width:90px"><b style="width:${(s.gen || 0) / maxGen * 100}%;background:${catOf(s.category).color}"></b></span>${num(s.gen, 1)}</td>
      <td class="num">${s.yoy == null ? '—' : `<span class="yoy ${s.yoy >= 0 ? 'up' : 'down'}">${s.yoy >= 0 ? '▲' : '▼'} ${num(Math.abs(s.yoy), 1)}%</span>`}</td>
      <td class="num">${s.plf == null ? '—' : num(s.plf, 2) + '%'}</td>
      <td class="num"><span class="bar"><b style="width:${Math.min(100, (s.achievement || 0) / 1.2)}%;background:${achColor(s.achievement)}"></b><span class="mark" style="left:${100 / 1.2}%"></span></span><span class="${achClass(s.achievement)}">${s.achievement == null ? '—' : num(s.achievement, 1) + '%'}</span></td>
    </tr>`).join('') + '</tbody>';
  $('league').querySelectorAll('th').forEach(th => th.onclick = () => { const k = th.dataset.k; SORT = { key: k, dir: SORT.key === k ? -SORT.dir : (k === 'name' ? 1 : -1) }; renderLeague(); });
  $('league').querySelectorAll('tbody tr').forEach(tr => tr.onclick = () => openStation(tr.dataset.st));
}

function renderUnits(){
  const u = DATA.units || [];
  $('unitsHint').textContent = 'FY ' + DATA.fy + ' · status as on ' + pretty(DATA.as_of);
  $('unitsTable').innerHTML = `<thead><tr><th>Unit</th><th class="num">MW</th><th class="num">Gen (MU)</th><th class="num">PLF</th><th class="num">PAF</th><th class="num">Trips</th><th>Now</th></tr></thead><tbody>`
    + u.map(x => `<tr><td><b style="color:var(--navy)">${esc(x.unit)}</b></td><td class="num">${num(x.capacity_mw, 1)}</td><td class="num">${num(x.gen, 1)}</td>
      <td class="num">${x.plf == null ? '—' : num(x.plf, 2) + '%'}</td><td class="num">${x.paf == null ? '—' : num(x.paf, 2) + '%'}</td><td class="num">${x.trips ?? '—'}</td>
      <td>${x.status === 'RUNNING' ? '<span class="st-pill run">Running</span>' : `<span class="st-pill out">${esc((x.status || '—').replace(/_/g, ' ').toLowerCase())}</span>`}</td></tr>`).join('') + '</tbody>';
}

function renderOut(){
  const list = DATA.units_out;
  $('outList').innerHTML = list.length ? list.map(u => `<div class="out-item"><div class="days"><b>${num(u.days, 0)}</b><span>days</span></div>
      <div><div class="u">${esc(u.unit)} · ${num(u.capacity_mw, 1)} MW</div><div class="d">${esc((u.status || '').replace(/_/g, ' ').toLowerCase())} since ${pretty(u.since)}<br>${esc(u.detail || '')}</div></div></div>`).join('')
    : `<div class="all-good">✓ All ${DATA.kpis.units_total} units were available.</div>`;
}

function renderCoal(){
  const c = DATA.coal;
  $('coalPanel').hidden = !c.length;
  if (!c.length) return;
  const max = Math.max(...c.map(x => Math.max(x.days || 0, x.norm || 0))) * 1.25;
  $('coalList').innerHTML = c.map(x => `<div class="coal-row"><div class="top"><b>${esc(x.station)}</b><span style="color:var(--faint);font-size:.7rem">${num(x.stock_mt, 0)} t in stock</span>
      <span class="days" style="color:${x.below ? 'var(--bad)' : 'var(--good)'}">${num(x.days, 1)} <small>days</small></span></div>
      <div class="gauge"><b style="width:0;background:${x.below ? 'linear-gradient(90deg,#c8372d,#f07a5f)' : 'linear-gradient(90deg,#1f9d57,#46d38a)'}" data-w="${(x.days || 0) / max * 100}"></b>
      <span class="norm" style="left:${x.norm / max * 100}%"></span></div>
      <div class="sub">${x.below ? 'Below' : 'Within'} the ${x.norm}-day normative stock</div></div>`).join('');
  requestAnimationFrame(() => document.querySelectorAll('#coalList .gauge b').forEach(b => b.style.width = b.dataset.w + '%'));
  const tr = DATA.coal_trend;
  chart('coalChart', { type: 'line', data: { labels: tr.map(x => x.label), datasets: [
      { label: 'Average days of stock', data: tr.map(x => x.days), borderColor: '#072577', backgroundColor: 'rgba(7,37,119,.1)', fill: true, tension: .35, pointRadius: 2, borderWidth: 2 },
      { label: 'Norm', data: tr.map(x => x.norm), borderColor: '#c8372d', borderDash: [5, 4], pointRadius: 0, borderWidth: 1.5 } ] },
    options: { responsive: true, maintainAspectRatio: false, animation: { duration: reduced ? 0 : 700 }, plugins: { legend: { display: false }, tooltip: { backgroundColor: '#04164a' } },
      scales: { x: { grid: { display: false }, ticks: { color: '#8a94a6', font: { family: 'Noto Sans', size: 10 } } }, y: { grid: { color: 'rgba(7,37,119,.06)' }, ticks: { color: '#8a94a6', font: { family: 'Noto Sans', size: 10 } } } } } });
}

function renderReservoirs(){
  const r = DATA.reservoirs;
  $('resPanel').hidden = !r.length;
  $('tanks').innerHTML = r.map(x => `<div class="tank"><div class="vessel"><div class="water" style="height:0" data-h="${Math.max(4, Math.min(100, x.pct || 0))}"></div>
      <div class="lvl">${num(x.pct, 0)}%</div></div><div class="nm">${esc((x.name || '').replace(/\b\w+/g, w => w[0] + w.slice(1).toLowerCase()))}</div>
      <div class="ft">${num(x.level, 1)} / ${num(x.frl, 1)} ${esc((x.unit || '').toLowerCase())}<br>feeds ${esc(x.feeds || '')}</div></div>`).join('');
  requestAnimationFrame(() => setTimeout(() => document.querySelectorAll('#tanks .water').forEach(w => w.style.height = w.dataset.h + '%'), 60));
}

function renderLosses(){
  const l = DATA.losses, max = Math.max(...l.map(x => x.loss || 0), 1), total = l.reduce((a, b) => a + (b.loss || 0), 0);
  $('lossHint').textContent = l.length ? `FY ${DATA.fy} · top reasons, ${num(total, 0)} MU in all` : `FY ${DATA.fy}`;
  $('lossList').innerHTML = l.length ? l.map(x => `<div class="loss-row"><span class="nm" title="${esc(x.reason)}">${esc(x.reason)}</span>
      <span class="track"><b class="${x.class === 'OUTAGE' ? 'outage' : ''}" style="width:0" data-w="${x.loss / max * 100}"></b></span><span class="mu">${num(x.loss, 1)} MU</span></div>`).join('')
    : '<div class="all-good">No generation losses recorded for this period.</div>';
  requestAnimationFrame(() => document.querySelectorAll('#lossList .track b').forEach(b => b.style.width = b.dataset.w + '%'));
}

function renderTrend(){
  const d = DATA, s = d.series, st = d.scope;
  $('trendTitle').textContent = 'Ten-year view';
  $('trendHint').textContent = st ? 'Generation (MU) and PLF (%) by financial year' : 'Generation (MU) and CEA target achievement (%)';
  if (!window.Chart) return;
  const labels = s.gen.map(x => x.fy);
  const second = st ? s.plf.map(x => x.v) : s.achievement.map(x => x.v);
  chart('trendChart', { data: { labels, datasets: [
      { type: 'bar', label: 'Generation (MU)', data: s.gen.map(x => x.v), backgroundColor: labels.map(f => f === d.fy ? '#e9b949' : 'rgba(7,37,119,.75)'), borderRadius: 5, maxBarThickness: 30, yAxisID: 'y' },
      { type: 'line', label: st ? 'PLF (%)' : 'CEA target achievement (%)', data: second, borderColor: '#2f8fd6', backgroundColor: '#2f8fd6', borderWidth: 2.5, pointRadius: 3, tension: .35, yAxisID: 'y1' } ] },
    options: { responsive: true, maintainAspectRatio: false, animation: { duration: reduced ? 0 : 800 }, interaction: { mode: 'index', intersect: false },
      plugins: { legend: { labels: { color: '#5f6b7d', font: FONT, boxWidth: 12 } }, tooltip: { backgroundColor: '#04164a' } },
      scales: { x: { grid: { display: false }, ticks: { color: '#5f6b7d', font: FONT } },
                y: { beginAtZero: true, grid: { color: 'rgba(7,37,119,.06)' }, border: { display: false }, ticks: { color: '#5f6b7d', font: FONT, callback: v => num(v, 0) } },
                y1: { position: 'right', min: st ? 0 : 80, max: st ? 100 : 110, grid: { display: false }, border: { display: false }, ticks: { color: '#2f8fd6', font: FONT, callback: v => v + '%' } } } } });
}

// ------------------------------------------------------------------ MIS report (Excel): the current selection, downloaded
// The workbook is built on the server from the same queries as this page, for the FY, station and Monthly / Weekly / Daily
// view that are selected right now ("As on screen"), or with all three periods ("Complete report").
const GRAIN_NAME = { monthly: 'Monthly', weekly: 'Weekly', daily: 'Daily' };
function misScopeText(){ const d = DATA; return d ? `${d.scope ? d.scope.station + ' · ' + d.scope.name : 'KPCL · all stations'} · FY ${d.fy}` : '—'; }
function updateMis(){
  const ready = !!DATA;
  $('misBtn').disabled = !ready || $('misBtn').classList.contains('busy');
  $('misView').disabled = !ready;
  $('misView').title = ready ? `View the ${GRAIN_NAME[GRAIN].toLowerCase()} MIS report for ${misScopeText()} before downloading it` : '';
  $('misScope').textContent = misScopeText();
  $('misOnScreenSub').textContent = ready ? `${GRAIN_NAME[GRAIN]} generation, summary, ${DATA.scope ? 'unit' : 'station'} tables, trend, losses` : '—';
  $('misBtn').title = ready ? `Download the ${GRAIN_NAME[GRAIN].toLowerCase()} MIS report for ${misScopeText()}` : '';
}
let toastTimer = null;
function toast(msg, err = false){
  const el = $('toast'); $('toastMsg').textContent = msg; el.classList.toggle('err', err); el.firstElementChild.textContent = err ? '!' : '✓';
  el.classList.add('on'); clearTimeout(toastTimer); toastTimer = setTimeout(() => el.classList.remove('on'), err ? 5000 : 3200);
}
function setMisMenu(open){ $('misMenu').hidden = !open; $('misMore').setAttribute('aria-expanded', open); }
function misQuery(kind){
  const qs = new URLSearchParams({ fy: DATA.fy, grain: kind === 'all' ? 'all' : GRAIN }); if (DATA.scope) qs.set('station', DATA.scope.station);
  return qs.toString();
}
// View: the same workbook in the on-screen viewer, with its own Download button (report-viewer.js)
function viewMis(kind, opener){
  if (!DATA || !window.ReportViewer) return;
  setMisMenu(false);
  const q = misQuery(kind);
  ReportViewer.open({ viewUrl: '/api/mis-report/view?' + q, opener, onDownload: () => downloadMis(kind, true) });
}
async function downloadMis(kind, fromViewer = false){
  if (!DATA) return;
  const btn = $('misBtn');
  setMisMenu(false); btn.classList.add('busy'); btn.disabled = true; if (!fromViewer) toast(t('misBusy'));
  try {
    const r = await fetch('/api/mis-report?' + misQuery(kind));
    if (r.status === 409){ const b = await r.json().catch(() => ({})); const e = new Error('under review'); e.review = b.message || ''; throw e; }
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const blob = await r.blob();
    const m = /filename="?([^";]+)"?/.exec(r.headers.get('Content-Disposition') || '');
    const name = m ? m[1] : `KPCL_MIS_FY${DATA.fy}.xlsx`;
    const url = URL.createObjectURL(blob), a = document.createElement('a');
    a.href = url; a.download = name; document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
    toast(`${t('misDone')} · ${name}`);
  } catch (e) {
    if (window.console) console.error(e);
    toast(e && e.review ? e.review : t('misFail'), true);
    if (fromViewer) throw e;
  } finally { btn.classList.remove('busy'); updateMis(); }
}
$('misBtn').onclick = () => downloadMis('current');
$('misMore').onclick = e => { e.stopPropagation(); setMisMenu($('misMenu').hidden); };
document.querySelectorAll('.mis-opt').forEach(b => b.onclick = () => downloadMis(b.dataset.kind));
document.querySelectorAll('.mis-opt-view').forEach(b => b.onclick = () => viewMis(b.dataset.kind, $('misView')));
$('misView').onclick = () => viewMis('current', $('misView'));
document.addEventListener('click', e => { if (!$('mis').contains(e.target)) setMisMenu(false); });
document.addEventListener('keydown', e => { if (e.key === 'Escape' && !$('misMenu').hidden){ setMisMenu(false); $('misMore').focus(); } });

// ------------------------------------------------------------------ station navigation (sidebar) + drill-down
function renderStationNav(){
  const idx = DATA ? DATA.stations_index : [];
  const groups = {}; idx.forEach(s => (groups[s.category] = groups[s.category] || []).push(s));
  $('stationNav').innerHTML = Object.entries(groups).map(([c, list]) => `<div class="side-group"><h3>${esc(catOf(c).label)}</h3>`
    + list.map(s => `<button type="button" class="st-item" data-st="${esc(s.station)}" title="${esc(s.name)}"><i style="background:${catOf(c).color}"></i>
        <span class="k">${esc(s.station)}</span><span class="n">${esc(s.name)}</span>${s.has_unit_out ? '<span class="dot-out" title="unit out"></span>' : ''}</button>`).join('') + '</div>').join('');
  $('stationNav').querySelectorAll('.st-item').forEach(b => b.onclick = () => { document.body.classList.remove('sidebar-open'); openStation(b.dataset.st); });
}
function openStation(key){ STATION = key === STATION ? null : key; $('main').scrollTo({ top: 0, behavior: reduced ? 'auto' : 'smooth' }); load(); }
$('backBtn').onclick = () => { STATION = null; load(); };
$('navOverview').onclick = e => { e.preventDefault(); document.body.classList.remove('sidebar-open'); STATION = null; load(); };
$('fySelect').onchange = e => { FY = e.target.value; load(); };
window.addEventListener('popstate', () => { const p = new URLSearchParams(location.search); FY = p.get('fy'); STATION = p.get('station'); load(false); });

// ------------------------------------------------------------------ AI mode transition (Cescom pattern)
document.querySelectorAll('.ai-link').forEach(a => a.addEventListener('click', e => {
  e.preventDefault();
  const ov = $('overlay'); $('overlayTitle').textContent = lang === 'kn' ? 'AI ಮೋಡ್' : 'Entering AI Mode'; $('overlaySub').textContent = lang === 'kn' ? 'ಕ.ವಿ.ನಿ.ನಿ ಉತ್ಪಾದನಾ ಸಹಾಯಕ' : 'KPCL Generation Assistant';
  ov.classList.remove('off');
  try { sessionStorage.setItem('kpcl-splash', '1'); } catch (_e) {}
  setTimeout(() => { location.href = a.getAttribute('href'); }, reduced ? 120 : 900);
}));

// Page links (Insights): the same logo transition as AI mode, then the real navigation.
document.querySelectorAll('.page-link').forEach(a => a.addEventListener('click', e => {
  e.preventDefault();
  const ov = $('overlay');
  $('overlayTitle').textContent = t(a.dataset.label); $('overlaySub').textContent = 'KPCL · Generation Management System';
  ov.classList.remove('off');
  try { sessionStorage.setItem('kpcl-splash', '1'); } catch (_e) {}
  setTimeout(() => { location.href = a.getAttribute('href'); }, reduced ? 120 : 900);
}));

// ------------------------------------------------------------------ language + drawer
function applyLang(l){
  lang = l; document.documentElement.lang = l;
  document.querySelectorAll('[data-i18n]').forEach(el => el.textContent = t(el.dataset.i18n));
  document.querySelectorAll('[data-i18n-html]').forEach(el => el.innerHTML = t(el.dataset.i18nHtml));
  $('langEn').setAttribute('aria-pressed', l === 'en'); $('langKn').setAttribute('aria-pressed', l === 'kn');
  try { localStorage.setItem('kpcl-lang', l); } catch (_e) {}
  if (DATA) render();
}
$('langEn').onclick = () => applyLang('en'); $('langKn').onclick = () => applyLang('kn');
$('sidebarToggle').onclick = () => document.body.classList.toggle('sidebar-open');
$('drawerBackdrop').onclick = () => document.body.classList.remove('sidebar-open');

// ------------------------------------------------------------------ start
(function(){
  let seen = null; try { seen = sessionStorage.getItem('kpcl-splash'); } catch (_e) {}
  const ov = $('overlay');
  if (seen){ ov.classList.add('off'); } else { try { sessionStorage.setItem('kpcl-splash', '1'); } catch (_e) {} setTimeout(() => ov.classList.add('off'), reduced ? 150 : 1300); }
})();
let savedLang = null; try { savedLang = localStorage.getItem('kpcl-lang'); } catch (_e) {}
applyLang(savedLang === 'kn' ? 'kn' : 'en');
{ const p = new URLSearchParams(location.search); FY = p.get('fy'); STATION = p.get('station'); }
load(false);
