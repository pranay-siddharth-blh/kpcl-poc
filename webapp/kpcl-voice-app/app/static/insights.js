// insights.html: page script (served from a file so the security policy allows no inline scripts)
// ------------------------------------------------------------------ labels (the insight sentences come from the server in English)
const I18N = {
  en: { govLine1: 'Government of Karnataka', govLine2: 'Karnataka Power Corporation Limited',
    appBar: '<b>Generation Management System</b><span class="extra"><span class="sep">|</span>Insights<span class="sep">|</span>April 2015 – April 2025</span>',
    aiBadge: 'AI MODE', menu: 'Menu', navDash: 'Dashboard', navInsights: 'Insights', navChat: 'Ask the assistant', askAi: 'Ask AI about this',
    eyebrow: 'Insights', title: 'Key insights', goDash: 'Opening the dashboard', goInsights: 'Opening Insights', goAi: 'Entering AI Mode',
    all: 'All', bad: 'Needs attention', good: 'Going well', info: 'For information',
    footNote: 'Every figure comes from the same reviewed, read-only queries as the dashboard. Proof-of-concept data; only the RTPS and SGS figures pinned to K-eRP reports are real.',
    groups: { 'Performance': 'Performance', 'Targets': 'Targets', 'Reliability': 'Reliability', 'Fuel and water': 'Fuel and water', 'Efficiency and grid': 'Efficiency and grid' } },
  kn: { govLine1: 'ಕರ್ನಾಟಕ ಸರ್ಕಾರ', govLine2: 'ಕರ್ನಾಟಕ ವಿದ್ಯುತ್ ನಿಗಮ ನಿಯಮಿತ',
    appBar: '<b>ಉತ್ಪಾದನಾ ನಿರ್ವಹಣಾ ವ್ಯವಸ್ಥೆ</b><span class="extra"><span class="sep">|</span>ಒಳನೋಟಗಳು<span class="sep">|</span>ಏಪ್ರಿಲ್ 2015 – ಏಪ್ರಿಲ್ 2025</span>',
    aiBadge: 'AI ಮೋಡ್', menu: 'ಮೆನು', navDash: 'ಡ್ಯಾಶ್‌ಬೋರ್ಡ್', navInsights: 'ಒಳನೋಟಗಳು', navChat: 'ಸಹಾಯಕನನ್ನು ಕೇಳಿ', askAi: 'AI ಅನ್ನು ಕೇಳಿ',
    eyebrow: 'ಒಳನೋಟಗಳು', title: 'ಮುಖ್ಯ ಒಳನೋಟಗಳು', goDash: 'ಡ್ಯಾಶ್‌ಬೋರ್ಡ್ ತೆರೆಯಲಾಗುತ್ತಿದೆ', goInsights: 'ಒಳನೋಟಗಳನ್ನು ತೆರೆಯಲಾಗುತ್ತಿದೆ', goAi: 'AI ಮೋಡ್',
    all: 'ಎಲ್ಲಾ', bad: 'ಗಮನ ಬೇಕು', good: 'ಉತ್ತಮವಾಗಿದೆ', info: 'ಮಾಹಿತಿಗಾಗಿ',
    footNote: 'ಪ್ರತಿ ಅಂಕಿಅಂಶವೂ ಡ್ಯಾಶ್‌ಬೋರ್ಡ್‌ನ ಅದೇ ಪರಿಶೀಲಿಸಿದ, ಓದಲು-ಮಾತ್ರ ಪ್ರಶ್ನೆಗಳಿಂದ ಬರುತ್ತದೆ.',
    groups: { 'Performance': 'ಕಾರ್ಯಕ್ಷಮತೆ', 'Targets': 'ಗುರಿಗಳು', 'Reliability': 'ವಿಶ್ವಾಸಾರ್ಹತೆ', 'Fuel and water': 'ಇಂಧನ ಮತ್ತು ನೀರು', 'Efficiency and grid': 'ದಕ್ಷತೆ ಮತ್ತು ಗ್ರಿಡ್' } },
};
let lang = 'en';
const t = k => I18N[lang][k] ?? I18N.en[k];
const CAT = {
  'THERMAL-COAL': { label: 'Thermal (coal)', color: '#072577' }, 'MAJOR HYDRO': { label: 'Major hydro', color: '#2f8fd6' },
  'MINOR HYDRO': { label: 'Minor hydro', color: '#8cc1ea' }, 'GAS': { label: 'Gas', color: '#8e6819' },
  'SOLAR': { label: 'Solar', color: '#e9b949' }, 'WIND': { label: 'Wind', color: '#2a9d8f' }, 'RES': { label: 'Waste to energy', color: '#7a8699' },
};
const catOf = c => CAT[c] || { label: c, color: '#7a8699' };
const TONE = { good: { accent: '#1f9d57', soft: '#e5f5ec' }, bad: { accent: '#c8372d', soft: '#fcebea' }, info: { accent: '#b8892b', soft: '#f7f1e3' } };
const IC = {
  bolt: '<path d="M13 2L4 14h7l-1 8 9-12h-7z"/>', gauge: '<path d="M12 14l4-4"/><path d="M3.3 17a9 9 0 1 1 17.4 0"/><circle cx="12" cy="14" r="1.2"/>',
  drop: '<path d="M12 2.7C9 7 6 10.3 6 14a6 6 0 0 0 12 0c0-3.7-3-7-6-11.3z"/>', trophy: '<path d="M8 21h8M12 17v4M7 4h10v5a5 5 0 0 1-10 0z"/><path d="M17 5h3v2a3 3 0 0 1-3 3M7 5H4v2a3 3 0 0 0 3 3"/>',
  up: '<path d="M3 17l6-6 4 4 8-8"/><path d="M15 7h6v6"/>', down: '<path d="M3 7l6 6 4-4 8 8"/><path d="M21 11v6h-6"/>',
  calendar: '<rect x="3" y="4" width="18" height="17" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>',
  target: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>', check: '<circle cx="12" cy="12" r="9"/><path d="M8 12l3 3 5-6"/>',
  power: '<path d="M18.4 6.6a9 9 0 1 1-12.8 0"/><path d="M12 2v10"/>', alert: '<path d="M12 3l9 16H3z"/><path d="M12 10v4M12 17h.01"/>',
  zap: '<path d="M13 2L4 14h7l-1 8 9-12h-7z"/>', loss: '<path d="M3 3v18h18"/><path d="M7 7l5 5 3-3 5 5"/>',
  coal: '<path d="M4 18l3-7 5 2 3-6 5 11z"/>', plug: '<path d="M9 2v6M15 2v6M6 8h12v4a6 6 0 0 1-12 0z"/><path d="M12 18v4"/>',
  grid: '<path d="M4 20L12 3l8 17"/><path d="M7 13h10M6 17h12"/>',
};
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const num = (v, d = 1) => v == null ? '—' : Number(v).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
const pretty = d => { if (!d) return '—'; const [y, m, dd] = d.slice(0, 10).split('-'); return `${dd}-${'JanFebMarAprMayJunJulAugSepOctNovDec'.slice((m - 1) * 3, m * 3)}-${y}`; };
const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
function countUp(el, to, d, ms = 1000){
  if (to == null){ el.textContent = '—'; return; }
  if (reduced){ el.textContent = num(to, d); return; }
  const t0 = performance.now();
  const step = now => { const p = Math.min(1, (now - t0) / ms), e = 1 - Math.pow(1 - p, 3); el.textContent = num(to * e, d); if (p < 1) requestAnimationFrame(step); };
  requestAnimationFrame(step);
}

let DATA = null, FY = null, FILTER = 'all';
const charts = {};

async function load(push = true){
  $('error').hidden = true;
  const qs = new URLSearchParams(); if (FY) qs.set('fy', FY);
  try {
    const r = await fetch('/api/insights?' + qs.toString());
    if (!r.ok){ const b = await r.json().catch(() => ({})); throw new Error(b.detail || ('HTTP ' + r.status)); }
    DATA = await r.json(); FY = DATA.fy;
    if (push){ const u = new URL(location); u.search = ''; if (FY !== DATA.complete_fys.at(-1)) u.searchParams.set('fy', FY); history.pushState({ fy: FY }, '', u); }
    render(); $('skeleton').hidden = true; $('body').hidden = false;
  } catch (e) {
    if (window.console) console.error(e);
    $('skeleton').hidden = true; $('error').textContent = 'The insights could not be loaded. ' + (e.message || ''); $('error').hidden = false;
  }
}

function render(){
  const d = DATA;
  if (!$('stationNav').children.length) renderStationNav();
  $('subline').innerHTML = `FY <b>${esc(d.fy)}</b>${d.is_partial ? ' (incomplete year)' : ''}${d.prev_fy && !d.is_partial ? ' · compared with FY ' + esc(d.prev_fy) : ''} · status as on <b>${pretty(d.as_of)}</b>`;
  $('fySelect').innerHTML = d.fys.slice().reverse().map(f => `<option value="${esc(f)}" ${f === d.fy ? 'selected' : ''}>FY ${esc(f)}${d.complete_fys.includes(f) ? '' : ' (part)'}</option>`).join('');
  const s = d.summary, total = d.cards.length;
  $('summary').innerHTML = ['all', 'bad', 'good', 'info'].map(k => {
    const n = k === 'all' ? total : s[k];
    return `<button type="button" class="ins-count ${k}" data-f="${k}" aria-pressed="${FILTER === k}"><b>${n}</b><span>${esc(t(k))}</span></button>`;
  }).join('');
  $('summary').querySelectorAll('button').forEach(b => b.onclick = () => { FILTER = b.dataset.f; render(); });
  const groups = [...new Set(d.cards.map(c => c.group))];
  let i = 0;
  $('groups').innerHTML = groups.map(g => {
    const cards = d.cards.filter(c => c.group === g && (FILTER === 'all' || c.tone === FILTER));
    if (!cards.length) return '';
    return `<section class="ins-group"><h2>${esc(t('groups')[g] || g)}<span>${cards.length}</span></h2><div class="ins-grid">`
      + cards.map(c => card(c, i++)).join('') + '</div></section>';
  }).join('');
  document.querySelectorAll('#groups [data-count]').forEach(el => countUp(el, Number(el.dataset.count), Number(el.dataset.d)));
  requestAnimationFrame(() => document.querySelectorAll('#groups canvas[data-spark]').forEach(cv => spark(cv)));
}

function card(c, i){
  const tone = TONE[c.tone] || TONE.info;
  let chip = '';
  if (c.delta != null){
    const up = c.delta >= 0, good = c.good_up ? up : !up, cls = Math.abs(c.delta) < 0.05 ? 'flat' : good ? 'up' : 'down';
    chip = `<span class="chip ${cls}">${up ? '▲' : '▼'} ${num(Math.abs(c.delta), 1)}${c.delta_unit === 'pts' ? ' pts' : '%'}</span>`;
  }
  const value = c.value == null ? '<span>—</span>' : `<span data-count="${c.value}" data-d="${c.decimals}">—</span>`;
  const series = Array.isArray(c.series) && c.series.filter(v => v != null).length > 2 ? c.series : null;
  return `<article class="kpi ins-card ${c.tone}" style="--accent:${tone.accent};--accent-soft:${tone.soft};animation-delay:${Math.min(i, 16) * 45}ms">
    <div class="top"><span class="ico"><svg viewBox="0 0 24 24">${IC[c.icon] || IC.bolt}</svg></span><span class="t">${esc(c.topic)}</span>
      <span class="ins-tone">${esc(t(c.tone))}</span></div>
    <div class="v">${value}<small>${esc(c.unit)}</small></div>
    <div class="foot">${chip}</div>
    <p class="ins-text">${esc(c.text)}</p>
    ${series ? `<div class="spark"><canvas id="sp${i}" data-spark='${JSON.stringify(series)}'></canvas></div>` : ''}
  </article>`;
}

function spark(cv){
  if (!window.Chart) return;
  const values = JSON.parse(cv.dataset.spark), acc = getComputedStyle(cv.closest('.kpi')).getPropertyValue('--accent').trim();
  if (charts[cv.id]) charts[cv.id].destroy();
  const ctx = cv.getContext('2d'), g = ctx.createLinearGradient(0, 0, 0, 40); g.addColorStop(0, acc + '40'); g.addColorStop(1, 'rgba(255,255,255,0)');
  charts[cv.id] = new Chart(ctx, { type: 'line', data: { labels: values.map((_, i) => i), datasets: [{ data: values, borderColor: acc, backgroundColor: g, fill: true,
    borderWidth: 2, tension: .35, pointRadius: values.map((_, i) => i === values.length - 1 ? 3 : 0), pointBackgroundColor: acc }] },
    options: { responsive: true, maintainAspectRatio: false, animation: { duration: reduced ? 0 : 800 }, plugins: { legend: { display: false }, tooltip: { enabled: false } },
      scales: { x: { display: false }, y: { display: false } } } });
}

// ------------------------------------------------------------------ sidebar stations (open the station's dashboard)
function renderStationNav(){
  const groups = {}; (DATA.stations_index || []).forEach(s => (groups[s.category] = groups[s.category] || []).push(s));
  $('stationNav').innerHTML = Object.entries(groups).map(([c, list]) => `<div class="side-group"><h3>${esc(catOf(c).label)}</h3>`
    + list.map(s => `<button type="button" class="st-item" data-st="${esc(s.station)}" title="${esc(s.name)}"><i style="background:${catOf(c).color}"></i>
        <span class="k">${esc(s.station)}</span><span class="n">${esc(s.name)}</span>${s.has_unit_out ? '<span class="dot-out" title="unit out"></span>' : ''}</button>`).join('') + '</div>').join('');
  $('stationNav').querySelectorAll('.st-item').forEach(b => b.onclick = () => go('/dashboard?station=' + encodeURIComponent(b.dataset.st), t('goDash')));
}

// ------------------------------------------------------------------ transitions (same look as the dashboard and chat)
function go(href, label){
  const ov = $('overlay'); $('overlayTitle').textContent = label; $('overlaySub').textContent = 'KPCL · Generation Management System';
  ov.classList.remove('off'); document.body.classList.remove('sidebar-open');
  try { sessionStorage.setItem('kpcl-splash', '1'); } catch (_e) {}
  setTimeout(() => { location.href = href; }, reduced ? 120 : 900);
}
document.querySelectorAll('.ai-link').forEach(a => a.addEventListener('click', e => { e.preventDefault(); go(a.getAttribute('href'), t('goAi')); }));
document.querySelectorAll('.page-link').forEach(a => a.addEventListener('click', e => { e.preventDefault(); go(a.getAttribute('href'), t(a.dataset.label)); }));
$('fySelect').onchange = e => { FY = e.target.value; load(); };
window.addEventListener('popstate', () => { FY = new URLSearchParams(location.search).get('fy'); load(false); });

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

(function(){
  let seen = null; try { seen = sessionStorage.getItem('kpcl-splash'); } catch (_e) {}
  const ov = $('overlay');
  if (seen){ ov.classList.add('off'); } else { try { sessionStorage.setItem('kpcl-splash', '1'); } catch (_e) {} setTimeout(() => ov.classList.add('off'), reduced ? 150 : 1300); }
})();
let savedLang = null; try { savedLang = localStorage.getItem('kpcl-lang'); } catch (_e) {}
applyLang(savedLang === 'kn' ? 'kn' : 'en');
FY = new URLSearchParams(location.search).get('fy');
load(false);
