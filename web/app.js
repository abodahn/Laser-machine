/* T&C Laser Intelligence & Command Center — single-page UI (no build step, no CDN). */
'use strict';

let ME = null, TIMER = null, CACHE = {};

/* ------------------------------------------------------------------- api */
async function api(path, opts) {
  const r = await fetch('/api' + path, Object.assign({headers: {'Content-Type': 'application/json'}}, opts));
  if (r.status === 401) { showLogin(); throw new Error('unauthenticated'); }
  if (!r.ok) { let d; try { d = (await r.json()).detail; } catch (e) { d = r.statusText; } throw new Error(d); }
  return r.json();   // every JSON route returns JSON; file exports go through window.open, not here
}
const get = p => api(p);
const post = (p, b) => api(p, {method: 'POST', body: JSON.stringify(b || {})});
const put = (p, b) => api(p, {method: 'PUT', body: JSON.stringify(b || {})});
const del = p => api(p, {method: 'DELETE'});

/* ---------------------------------------------------------------- helpers */
const $ = s => document.querySelector(s);
const el = (h) => { const d = document.createElement('div'); d.innerHTML = h.trim(); return d.firstChild; };
const esc = s => (s === null || s === undefined ? '' : String(s)).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
const n0 = v => v === null || v === undefined || isNaN(v) ? '&mdash;' : Math.round(v).toLocaleString();
const n1 = v => v === null || v === undefined || isNaN(v) ? '&mdash;' : (+v).toFixed(1);
const pct = v => v === null || v === undefined || isNaN(v) ? '&mdash;' : (+v).toFixed(1) + '%';
const dur = s => { if (s === null || s === undefined) return '&mdash;'; s = +s; if (s < 90) return Math.round(s) + 's'; if (s < 5400) return Math.round(s / 60) + 'm'; if (s < 172800) return (s / 3600).toFixed(1) + 'h'; return (s / 86400).toFixed(1) + 'd'; };
const ago = ts => { if (!ts) return 'never'; const d = (Date.now() - new Date(ts.replace(' ', 'T')).getTime()) / 1000; return d < 0 ? 'now' : dur(d) + ' ago'; };
const today = () => new Date().toISOString().slice(0, 10);
const daysAgo = d => new Date(Date.now() - d * 864e5).toISOString().slice(0, 10);
const can = p => ME && ME.permissions.includes(p);

function toast(msg, kind) {
  const node = el(`<div class="toast ${kind || ''}">${esc(msg)}</div>`);
  document.body.appendChild(node); setTimeout(() => node.remove(), 4200);
}
function busy(on) { document.body.style.cursor = on ? 'progress' : ''; }

/* ------------------------------------------------------- auto-refresh ---- */
/* Dashboards rebuild their DOM on a timer. Do it without throwing the user's
   scroll position away, pause while they are busy, and let them pick the rate. */
const REFRESH_CHOICES = [[0, 'Off'], [15000, '15s'], [30000, '30s'], [60000, '1m'], [300000, '5m']];
let REFRESH_MS = localStorage.refreshMs === undefined ? 30000 : +localStorage.refreshMs;

function setRefresh(ms) {
  REFRESH_MS = +ms; localStorage.refreshMs = REFRESH_MS;
  toast(REFRESH_MS ? `Auto-refresh every ${REFRESH_CHOICES.find(c => c[0] === REFRESH_MS)[1]}` : 'Auto-refresh off', 'ok');
  route();                                    // restart the page timer at the new rate
}

/* Replace #view content but keep where the user was looking. */
function paint(html) {
  const view = $('#view');
  const y = window.scrollY;
  const scrolls = [...view.querySelectorAll('.tw')].map(e => e.scrollTop);
  view.innerHTML = html;
  const tws = view.querySelectorAll('.tw');
  scrolls.forEach((t, i) => { if (tws[i]) tws[i].scrollTop = t; });
  window.scrollTo(0, y);
}

/* Skip a scheduled refresh while the user is mid-interaction. */
function refreshBlocked() {
  return document.hidden || !!$('#modal') || !!document.querySelector('#view input:focus, #view select:focus');
}

function autoRefresh(fn) {
  if (TIMER) { clearInterval(TIMER); TIMER = null; }
  if (!REFRESH_MS) return;
  TIMER = setInterval(() => { if (!refreshBlocked()) fn(); }, REFRESH_MS);
}

/* ------------------------------------------------------------------ i18n */
/* Three languages, matching the TC Platform convention: en / ar / tr, flat
   dot-notation keys. Arabic renders right-to-left, which is a layout change,
   not just a string swap — see the [dir="rtl"] rules in style.css. */
const LANGS = { en: 'English', ar: 'العربية', tr: 'Türkçe' };
const RTL_LANGS = new Set(['ar']);
let LANG = localStorage.lang || 'en';
let STRINGS = {};

async function loadLang(code) {
  if (!LANGS[code]) code = 'en';
  try {
    const r = await fetch(`/static/i18n/${code}.json?v=20`);
    STRINGS = r.ok ? await r.json() : {};
  } catch (e) { STRINGS = {}; }
  LANG = code;
  localStorage.lang = code;
  applyDirection();
}

function applyDirection() {
  const rtl = RTL_LANGS.has(LANG);
  document.documentElement.lang = LANG;
  document.documentElement.dir = rtl ? 'rtl' : 'ltr';
}

/* t('laser.kpi.units_today') -> the translated label.
   Falls back to English, then to the key itself, so a missing translation
   degrades to readable text instead of blanking the UI. */
function t(key, vars) {
  let v = STRINGS[key];
  if (v === undefined) v = EN_FALLBACK[key];
  if (v === undefined) return key;
  if (vars) for (const k in vars) v = v.split('{' + k + '}').join(vars[k]);
  return v;
}

let EN_FALLBACK = {};
async function initI18n() {
  try {
    const r = await fetch('/static/i18n/en.json?v=20');
    EN_FALLBACK = r.ok ? await r.json() : {};
  } catch (e) { EN_FALLBACK = {}; }
  await loadLang(LANG);
}

async function setLang(code) {
  await loadLang(code);
  renderNav();
  route();
  tick();
  toast(t('laser.lbl.language') + ': ' + LANGS[code], 'ok');
}

/* status / severity words come from the catalogue so badges translate too */
const tStatus = s => t('laser.status.' + (s || 'UNKNOWN'), null) || s;
const tSev = s => t('laser.sev.' + (s || 'info'), null) || s;
/* alert types are snake_case in the DB; unknown ones degrade to readable words */
const tAlertType = ty => { const v = t('laser.atype.' + ty); return v.slice(0, 6) === 'laser.' ? String(ty || '').replace(/_/g, ' ') : v; };
const SEV_RANK = {critical: 0, serious: 1, warning: 2, info: 3, ok: 4};
const sevRank = s => SEV_RANK[s] === undefined ? 3 : SEV_RANK[s];

/* --------------------------------------------------- critical alarm siren */
/* A synthesized two-tone siren (no audio files). It repeats until every
   critical alert is acknowledged or the user silences it. */
// Opt-in, not opt-out: a siren that starts on its own in someone's office is a
// nuisance. Turn it on deliberately on the wall-display PC, where it belongs.
let SOUND_ON = localStorage.soundOn === '1';
let AC = null, KNOWN_CRIT = null, SILENCED = new Set(), sirenTimer = null;

function audioCtx() {
  if (!AC) { try { AC = new (window.AudioContext || window.webkitAudioContext)(); } catch (e) { AC = null; } }
  if (AC && AC.state === 'suspended') AC.resume();
  return AC;
}
document.addEventListener('click', () => audioCtx(), { once: true });

function sirenBlast() {
  const ac = audioCtx();
  if (!ac) return;
  const t0 = ac.currentTime;
  for (let i = 0; i < 3; i++) {                        // three rising/falling wails
    const o = ac.createOscillator(), g = ac.createGain();
    o.type = 'sawtooth';
    const s = t0 + i * 0.55;
    o.frequency.setValueAtTime(660, s);
    o.frequency.linearRampToValueAtTime(1180, s + 0.28);
    o.frequency.linearRampToValueAtTime(660, s + 0.52);
    g.gain.setValueAtTime(0.0001, s);
    g.gain.exponentialRampToValueAtTime(0.6, s + 0.05);   // loud
    g.gain.exponentialRampToValueAtTime(0.0001, s + 0.52);
    o.connect(g); g.connect(ac.destination);
    o.start(s); o.stop(s + 0.54);
  }
}

function startSiren() {
  if (!SOUND_ON || sirenTimer) return;
  sirenBlast();
  sirenTimer = setInterval(sirenBlast, 4000);
  document.body.classList.add('crit-flash');
}
function stopSiren() {
  if (sirenTimer) { clearInterval(sirenTimer); sirenTimer = null; }
  document.body.classList.remove('crit-flash');
}

function checkCriticals(alerts) {
  const crit = alerts.filter(a => a.severity === 'critical' && a.status === 'open');
  const ids = new Set(crit.map(a => a.id));
  // first poll just seeds the baseline — do not blast on page load for pre-existing alerts
  if (KNOWN_CRIT === null) { KNOWN_CRIT = ids; }
  const unsilenced = crit.filter(a => !SILENCED.has(a.id));
  const bar = $('#critbar');
  if (unsilenced.length) {
    bar.classList.add('show');
    $('#critcount').textContent = unsilenced.length + (unsilenced.length === 1 ? ' CRITICAL' : ' CRITICAL');
    $('#critmsg').textContent = unsilenced.map(a => `${a.machine_name || ''} — ${a.title}`).join('   •   ');
    // any brand-new critical (re)arms the siren even if previously silenced others
    const fresh = crit.some(a => !KNOWN_CRIT.has(a.id));
    if (fresh) SILENCED = new Set([...SILENCED].filter(id => ids.has(id)));   // keep only still-open silences
    if (crit.filter(a => !SILENCED.has(a.id)).length) startSiren(); else stopSiren();
  } else {
    bar.classList.remove('show');
    stopSiren();
  }
  KNOWN_CRIT = ids;
  // prune silences for alerts that closed
  SILENCED = new Set([...SILENCED].filter(id => ids.has(id)));
}

function silence() {
  if (CACHE.crit) CACHE.crit.forEach(a => SILENCED.add(a.id));
  stopSiren();
  $('#critbar').classList.remove('show');
  toast('Critical alarm silenced until the next new one', 'ok');
}
async function ackAllCritical() {
  if (!can('ack')) { toast('You do not have permission to acknowledge alerts', 'bad'); return; }
  const crit = (CACHE.crit || []);
  for (const a of crit) { try { await post(`/alerts/${a.id}/ack`); } catch (e) { /* keep going */ } }
  stopSiren(); $('#critbar').classList.remove('show');
  toast(`Acknowledged ${crit.length} critical alert(s)`, 'ok');
  tick();
}
function toggleSound() {
  SOUND_ON = !SOUND_ON;
  localStorage.soundOn = SOUND_ON ? '1' : '0';
  const b = $('#soundbtn');
  b.textContent = SOUND_ON ? '🔔' : '🔕';
  b.title = 'Critical alarm sound: ' + (SOUND_ON ? 'on' : 'off');
  b.classList.toggle('armed', SOUND_ON);
  b.classList.toggle('muted', !SOUND_ON);
  if (!SOUND_ON) stopSiren(); else { audioCtx(); if (CACHE.crit && CACHE.crit.some(a => !SILENCED.has(a.id))) startSiren(); }
}

/* -------------------------------------------------------- extra charts */
function svgDonut(segments, opts) {                  // [{label,value,color}]
  opts = opts || {}; const total = segments.reduce((a, s) => a + (s.value || 0), 0);
  const R = 52, r = 32, cx = 60, cy = 60, C = 2 * Math.PI * R;
  if (!total) return `<div class="empty">No data</div>`;
  let off = 0, arcs = '';
  segments.forEach(s => {
    const frac = (s.value || 0) / total;
    if (frac <= 0) return;
    arcs += `<circle cx="${cx}" cy="${cy}" r="${R}" fill="none" stroke="${s.color}" stroke-width="${R - r}"
      stroke-dasharray="${frac * C} ${C}" stroke-dashoffset="${-off * C}" transform="rotate(-90 ${cx} ${cy})">
      <title>${esc(s.label)}: ${n0(s.value)} (${(frac * 100).toFixed(0)}%)</title></circle>`;
    off += frac;
  });
  const center = opts.center || n0(total);
  return `<div class="donutwrap"><svg viewBox="0 0 120 120" width="128" height="128">${arcs}
    <text x="60" y="58" text-anchor="middle" style="font-size:20px;fill:var(--txt);font-weight:700">${center}</text>
    <text x="60" y="74" text-anchor="middle" style="font-size:9px;fill:var(--dim)">${esc(opts.label || '')}</text></svg>
    <div>${segments.filter(s => s.value).map(s => `<div style="display:flex;align-items:center;gap:7px;padding:2px 0;font-size:12px">
      <i style="width:10px;height:10px;border-radius:3px;background:${s.color};display:inline-block"></i>
      <span style="color:var(--muted);flex:1">${esc(s.label)}</span><b>${n0(s.value)}</b></div>`).join('')}</div></div>`;
}

function sparkline(data, opts) {
  opts = opts || {}; const w = opts.w || 120, h = opts.h || 26, p = 2;
  const v = data.map(x => +x || 0);
  if (!v.length) return '';
  const max = Math.max(...v, 1), min = Math.min(...v, 0);
  const X = i => p + i * (w - 2 * p) / Math.max(1, v.length - 1);
  const Y = x => h - p - (x - min) / (max - min || 1) * (h - 2 * p);
  const pts = v.map((x, i) => `${X(i)},${Y(x)}`).join(' ');
  const c = opts.color || 'var(--accent)';
  const last = v[v.length - 1], first = v[0];
  const cc = opts.flat ? c : last >= first ? 'var(--ok)' : 'var(--bad)';
  return `<svg viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" preserveAspectRatio="none">
    <polygon points="${p},${h} ${pts} ${w - p},${h}" fill="${cc}" opacity=".13"/>
    <polyline points="${pts}" fill="none" stroke="${cc}" stroke-width="1.6"/>
    <circle cx="${X(v.length - 1)}" cy="${Y(last)}" r="2" fill="${cc}"/></svg>`;
}

function heatmap(hm) {
  const peak = hm.peak || 1;
  const cell = (v) => {
    const frac = Math.min(1, (v || 0) / peak);
    const bg = frac === 0 ? 'rgba(255,255,255,.04)'
      : `rgba(62,166,255,${(0.15 + frac * 0.8).toFixed(2)})`;
    return `<td><div class="cell" style="background:${bg}" title="${n1(v)} units avg"></div></td>`;
  };
  const hours = Array.from({ length: 24 }, (_, h) => h);
  return `<div class="chartwrap"><table class="heat"><thead><tr><th></th>${
    hours.map(h => `<th>${h % 3 === 0 ? (h < 10 ? '0' + h : h) : ''}</th>`).join('')}</tr></thead><tbody>${
    hm.grid.map((row, d) => `<tr><th style="text-align:right;padding-right:8px">${hm.weekdays[d]}</th>${
      row.map(cell).join('')}</tr>`).join('')}</tbody></table>
    <div class="legend"><span>Avg units/hour</span>
      <span><i style="background:rgba(255,255,255,.04)"></i>0</span>
      <span><i style="background:rgba(62,166,255,.5)"></i>mid</span>
      <span><i style="background:rgba(62,166,255,.95)"></i>${n0(peak)} peak</span></div></div>`;
}

/* ------------------------------------------------------------ svg charts */
// Chart identity only — never status. Fixed order, never cycled: a 9th series
// folds into 'Other' rather than inventing a hue. Validated for colour-vision
// deficiency against this surface (worst adjacent protan dE 8.4).
const PALETTE = ['#3987e5', '#d95926', '#199e70', '#c98500',
                 '#d55181', '#008300', '#9085e9', '#e66767'];

function svgLine(series, opts) {
  opts = opts || {}; const W = 900, H = opts.height || 210, P = {l: 44, r: 12, t: 12, b: 26};
  const labels = opts.labels || [];
  const all = series.flatMap(s => s.data).filter(v => v !== null && v !== undefined && !isNaN(v));
  if (!all.length) return `<div class="empty">No data for this period</div>`;
  let max = Math.max(...all, opts.min0 === false ? -Infinity : 0), min = opts.min0 === false ? Math.min(...all) : 0;
  if (max === min) max = min + 1;
  const nx = Math.max(1, (series[0].data.length - 1));
  const X = i => P.l + i * (W - P.l - P.r) / nx;
  const Y = v => H - P.b - (v - min) / (max - min) * (H - P.t - P.b);
  let g = '';
  for (let k = 0; k <= 4; k++) { const v = min + (max - min) * k / 4, y = Y(v);
    g += `<line x1="${P.l}" y1="${y}" x2="${W - P.r}" y2="${y}" stroke="var(--line2)" stroke-width="1"/>` +
         `<text x="${P.l - 6}" y="${y + 3}" text-anchor="end">${fmtAxis(v)}</text>`; }
  const step = Math.ceil(labels.length / 12) || 1;
  labels.forEach((l, i) => { if (i % step === 0) g += `<text x="${X(i)}" y="${H - 8}" text-anchor="middle">${esc(l)}</text>`; });
  series.forEach((s, si) => {
    const c = s.color || PALETTE[si % PALETTE.length];
    const pts = s.data.map((v, i) => (v === null || v === undefined || isNaN(v)) ? null : `${X(i)},${Y(v)}`).filter(Boolean);
    if (!pts.length) return;
    if (s.area) g += `<polygon points="${P.l},${Y(min)} ${pts.join(' ')} ${X(s.data.length - 1)},${Y(min)}" fill="${c}" opacity=".12"/>`;
    g += `<polyline points="${pts.join(' ')}" fill="none" stroke="${c}" stroke-width="2" stroke-linejoin="round"/>`;
    s.data.forEach((v, i) => { if (v !== null && v !== undefined && !isNaN(v)) g += `<circle cx="${X(i)}" cy="${Y(v)}" r="2.4" fill="${c}"><title>${esc(labels[i] || i)}: ${fmtAxis(v)}</title></circle>`; });
  });
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}">${g}</svg>` + legend(series);
}

function svgBar(labels, series, opts) {
  opts = opts || {}; const W = 900, H = opts.height || 210, P = {l: 44, r: 12, t: 12, b: opts.rotate ? 62 : 26};
  const all = series.flatMap(s => s.data).filter(v => v !== null && !isNaN(v));
  if (!all.length) return `<div class="empty">No data for this period</div>`;
  const stacked = !!opts.stacked;
  let max = stacked ? Math.max(...labels.map((_, i) => series.reduce((a, s) => a + (+s.data[i] || 0), 0))) : Math.max(...all, 0);
  if (max <= 0) max = 1;
  const iw = (W - P.l - P.r) / Math.max(1, labels.length), bw = stacked ? iw * .62 : iw * .62 / series.length;
  const Y = v => H - P.b - v / max * (H - P.t - P.b);
  let g = '';
  for (let k = 0; k <= 4; k++) { const v = max * k / 4, y = Y(v);
    g += `<line x1="${P.l}" y1="${y}" x2="${W - P.r}" y2="${y}" stroke="var(--line2)"/><text x="${P.l - 6}" y="${y + 3}" text-anchor="end">${fmtAxis(v)}</text>`; }
  labels.forEach((l, i) => {
    const x0 = P.l + i * iw + iw * .19; let acc = 0;
    series.forEach((s, si) => {
      const v = +s.data[i] || 0, c = s.color || PALETTE[si % PALETTE.length];
      const h = Math.max(0, (H - P.b) - Y(v));
      const x = stacked ? x0 : x0 + si * bw, y = stacked ? Y(acc + v) : Y(v);
      g += `<rect x="${x}" y="${y}" width="${bw - 1}" height="${h}" fill="${c}" rx="2"><title>${esc(l)} ${esc(s.name || '')}: ${fmtAxis(v)}</title></rect>`;
      acc += v;
    });
    const step = Math.ceil(labels.length / (opts.rotate ? 40 : 14)) || 1;
    if (i % step === 0) g += opts.rotate
      ? `<text x="${P.l + i * iw + iw / 2}" y="${H - 12}" text-anchor="end" transform="rotate(-40 ${P.l + i * iw + iw / 2} ${H - 12})">${esc(String(l).slice(0, 22))}</text>`
      : `<text x="${P.l + i * iw + iw / 2}" y="${H - 8}" text-anchor="middle">${esc(l)}</text>`;
  });
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}">${g}</svg>` + legend(series);
}

function legend(series) {
  if (series.length < 2 && !series[0].name) return '';
  return `<div class="legend">${series.map((s, i) => `<span><i style="background:${s.color || PALETTE[i % PALETTE.length]}"></i>${esc(s.name || '')}</span>`).join('')}</div>`;
}
function fmtAxis(v) { v = +v; if (Math.abs(v) >= 1e6) return (v / 1e6).toFixed(1) + 'M'; if (Math.abs(v) >= 1000) return (v / 1000).toFixed(1) + 'k'; return Math.abs(v) < 10 ? v.toFixed(1) : Math.round(v); }

function gauge(value, label) {
  const v = Math.max(0, Math.min(100, +value || 0)), r = 24, c = 2 * Math.PI * r;
  const col = v >= 80 ? 'var(--ok)' : v >= 60 ? 'var(--warn)' : 'var(--bad)';
  return `<div class="gauge"><svg class="g" viewBox="0 0 56 56"><circle cx="28" cy="28" r="${r}" fill="none" stroke="var(--line2)" stroke-width="6"/>
    <circle cx="28" cy="28" r="${r}" fill="none" stroke="${col}" stroke-width="6" stroke-linecap="round"
      stroke-dasharray="${c}" stroke-dashoffset="${c * (1 - v / 100)}" transform="rotate(-90 28 28)"/>
    <text x="28" y="32" text-anchor="middle" style="font-size:14px;fill:var(--txt);font-weight:600">${Math.round(v)}</text></svg>
    <div><div style="font-size:11px;color:var(--muted)">${esc(label || '')}</div></div></div>`;
}

/* --------------------------------------------------------------- tables */
function table(cols, rows, opts) {
  opts = opts || {};
  if (!rows || !rows.length) return `<div class="empty">${esc(opts.empty || 'No data')}</div>`;
  const head = cols.map((c, i) => `<th class="${c.num ? 'num' : ''}" onclick="sortTable(this,${i})">${esc(c.label)}</th>`).join('');
  const body = rows.map(r => `<tr ${opts.onclick ? `style="cursor:pointer" onclick="${opts.onclick(r)}"` : ''}>` +
    cols.map(c => `<td class="${c.num ? 'num' : ''}">${c.render ? c.render(r) : esc(r[c.key])}</td>`).join('') + '</tr>').join('');
  return `<div class="tw"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}
function sortTable(th, i) {
  const tb = th.closest('table').tBodies[0], dir = th.dataset.d === 'a' ? -1 : 1;
  th.dataset.d = dir === 1 ? 'a' : 'd';
  [...tb.rows].sort((x, y) => {
    const a = x.cells[i].innerText.replace(/[,%]/g, ''), b = y.cells[i].innerText.replace(/[,%]/g, '');
    const na = parseFloat(a), nb = parseFloat(b);
    return (isNaN(na) || isNaN(nb)) ? dir * a.localeCompare(b) : dir * (na - nb);
  }).forEach(r => tb.appendChild(r));
}

/* ----------------------------------------------------------------- shell */
const NAV = [
  {grp: 'laser.nav.overview'},
  {h: 'command', ic: '▦', k: 'laser.nav.command'},
  {h: 'management', ic: '▲', k: 'laser.nav.management'},
  {h: 'production', ic: '⚙', k: 'laser.nav.production'},
  {h: 'maintenance', ic: '⚒', k: 'laser.nav.maintenance'},
  {h: 'technical', ic: '⚡', k: 'laser.nav.technical'},
  {h: 'wall', ic: '▣', k: 'laser.nav.wall'},
  {grp: 'laser.nav.analysis'},
  {h: 'analytics', ic: '∑', k: 'laser.nav.analytics'},
  {h: 'history', ic: '◴', k: 'laser.nav.history'},
  {h: 'alerts', ic: '⚠', k: 'laser.nav.alerts'},
  {h: 'reports', ic: '☷', k: 'laser.nav.reports'},
  {grp: 'laser.nav.administration'},
  {h: 'machines', ic: '⌗', k: 'laser.nav.machines', perm: 'config'},
  {h: 'settings', ic: '⚙', k: 'laser.nav.settings'},
];

function renderNav() {
  $('#nav').innerHTML = NAV.filter(i => !i.perm || can(i.perm)).map(i =>
    i.grp ? `<div class="grp">${esc(t(i.grp))}</div>`
          : `<a href="#/${i.h}" data-h="${i.h}"><span class="ic">${i.ic}</span>${esc(t(i.k))}</a>`).join('');
  $('#whoami').innerHTML = `<b>${esc(ME.full_name || ME.username)}</b><br><span style="color:var(--dim)">${esc(ME.role)}</span>`;
}

const ROUTES = {
  command: pageCommand, management: pageManagement, production: pageProduction,
  maintenance: pageMaintenance, technical: pageTechnical, analytics: pageAnalytics,
  history: pageHistory, alerts: pageAlerts, reports: pageReports, machines: pageMachines,
  settings: pageSettings, machine: pageMachineDetail,
  // wall.js is a separate script; resolve it at call time so load order does not matter
  wall: (...a) => pageWall(...a),
};

async function route() {
  document.body.classList.remove('navopen');   // close the mobile drawer on navigate
  const parts = (location.hash.slice(2) || 'command').split('/');
  const fn = ROUTES[parts[0]] || pageCommand;
  document.querySelectorAll('#nav a').forEach(a => a.classList.toggle('on', a.dataset.h === parts[0]));
  if (TIMER) { clearInterval(TIMER); TIMER = null; }
  if (parts[0] !== 'wall' && document.body.classList.contains('wall')) {
    document.body.classList.remove('wall');
    if (typeof WALL !== 'undefined' && WALL.tick) { clearInterval(WALL.tick); WALL.tick = null; }
  }
  $('#view').innerHTML = '<div class="empty">Loading…</div>';
  try { await fn(parts.slice(1)); } catch (e) { $('#view').innerHTML = `<div class="empty">Failed to load: ${esc(e.message)}</div>`; }
}

async function tick() {
  try {
    const [ov, open] = await Promise.all([get('/overview'), get('/alerts?status=open&days=2')]);
    CACHE.ov = ov; const tot = ov.totals;
    CACHE.crit = open.filter(a => a.severity === 'critical' && a.status === 'open');
    $('#pill-live').innerHTML = `<b>${tot.machines_producing}</b> producing / <b>${tot.machines_online}</b> online / ${tot.machines_total}`;
    $('#pill-alerts').innerHTML = tot.critical_alerts
      ? `<b style="color:var(--bad)">${tot.critical_alerts} critical</b> · ${tot.open_alerts} open`
      : `<b>${tot.open_alerts}</b> open alerts`;
    const badge = document.querySelector('#nav a[data-h="alerts"]');
    if (badge) {
      const ex = badge.querySelector('.nb'); if (ex) ex.remove();
      if (tot.open_alerts) badge.insertAdjacentHTML('beforeend',
        `<span class="nb" style="background:${tot.critical_alerts ? 'var(--bad)' : 'var(--warn)'}">${tot.open_alerts}</span>`);
    }
    checkCriticals(open);
  } catch (e) {
    // Could not confirm the alert state. Fail SILENT, never stuck-loud: a siren the
    // server can no longer stop is worse than a missed alert, and the next successful
    // poll re-arms it within seconds if the condition is still real.
    stopSiren();
    $('#critbar').classList.remove('show');
  }
  $('#pill-clock').textContent = new Date().toLocaleTimeString();
}

/* ================================================================ PAGES == */

/* ---------------------------------------------------- 1. command center */
async function pageCommand() {
  $('#title').textContent = t('laser.page.command');
  const render = async () => {
    const [ov, att] = await Promise.all([get('/overview'), get('/attention')]);
    CACHE.ov = ov; const tot = ov.totals;
    paint(`
      <div class="kpis">
        ${kpi(t('laser.kpi.units_today'), n0(tot.units), tot.target ? `target ${n0(tot.target)} · ${pct(tot.target_pct)}` : `${n0(tot.jobs)} jobs`, tot.target_pct >= 95 ? 'ok' : tot.target_pct >= 80 ? 'warn' : tot.target ? 'bad' : 'accent')}
        ${kpi(t('laser.kpi.forecast_eod'), n0(tot.forecast_units), tot.target ? `vs target ${n0(tot.target)}` : 'projected units', 'accent')}
        ${kpi(t('laser.kpi.utilization'), pct(tot.utilization), 'busy vs planned time', tot.utilization >= 70 ? 'ok' : tot.utilization >= 50 ? 'warn' : 'bad')}
        ${kpi(t('laser.kpi.laser_duty'), pct(tot.laser_duty), 'laser on vs job time')}
        ${kpi(t('laser.kpi.machines_producing'), `${tot.machines_producing}/${tot.machines_total}`, `${tot.machines_online} online`, tot.machines_producing >= tot.machines_total * .8 ? 'ok' : 'warn')}
        ${kpi(t('laser.kpi.downtime'), n1(tot.down_hours) + ' h', `idle ${n1(tot.idle_hours)} h`, tot.down_hours > 8 ? 'bad' : 'warn')}
        ${kpi(t('laser.kpi.alarms_today'), n0(tot.alarms), 'machine alarms')}
        ${kpi(t('laser.kpi.open_alerts'), n0(tot.open_alerts), `${tot.critical_alerts} critical`, tot.critical_alerts ? 'bad' : tot.open_alerts ? 'warn' : 'ok')}
      </div>

      <div class="panel"><h3>${esc(t('laser.panel.machine_status'))} <span class="sub">${esc(ov.date)} — click a machine for full detail</span>
        <span class="right">${Object.entries(ov.status_counts).map(([k, v]) => `<span class="badge b-info">${esc(k)} ${v}</span>`).join(' ')}</span></h3>
        <div class="body"><div class="mgrid">${ov.machines.map(machineCard).join('')}</div></div></div>

      <div class="grid3">
        <div class="panel"><h3>${esc(t('laser.panel.fleet_status'))}</h3><div class="body" id="fleetdonut"></div></div>
        <div class="panel" style="grid-column:span 2"><h3>${esc(t('laser.panel.hourly_production'))} <span class="sub">${esc(t('laser.panel.hourly_sub'))}</span></h3>
          <div class="body" id="hourly">Loading…</div></div>
      </div>

      <div class="grid2">
        <div class="panel"><h3>${esc(t('laser.panel.needs_attention'))} <span class="sub">${esc(t('laser.panel.needs_attention_sub'))}</span></h3><div class="body">${
          att.length ? table([
            {label: t('laser.col.machine'), key: 'name'}, {label: t('laser.col.status'), key: 'status',
              render: r => `<span class="badge b-${r.status === 'OFFLINE' || r.status === 'STOPPED' ? 'critical' : 'warning'}">${esc(r.status || '-')}</span>`},
            {label: t('laser.col.score'), key: 'score', num: true},
            {label: t('laser.col.why'), key: 'reasons', render: r => esc(r.reasons.join(' · '))}],
            att, {onclick: r => `go('machine/${r.machine_id}')`})
          : '<div class="empty">✓ Everything is running normally</div>'}</div></div>
        <div class="panel"><h3>${esc(t('laser.panel.live_alerts'))}</h3><div class="body" id="livealerts">Loading…</div></div>
      </div>

      <div class="panel"><h3>${esc(t('laser.panel.production_rhythm'))} <span class="sub">${esc(t('laser.sub.average_units_by_weekday_and_hour_last_21_da'))}</span></h3>
        <div class="body" id="heatmap">Loading…</div></div>`);

    const statusColor = { PRODUCING: 'var(--ok)', IDLE: 'var(--warn)', STOPPED: 'var(--bad)',
      ALARM: 'var(--bad)', OFFLINE: 'var(--idle)', MAINTENANCE: 'var(--maint)', UNKNOWN: 'var(--idle)' };
    $('#fleetdonut').innerHTML = svgDonut(
      Object.entries(ov.status_counts).map(([k, v]) => ({ label: k, value: v, color: statusColor[k] || 'var(--idle)' })),
      { center: `${tot.machines_total}`, label: 'machines' });

    get('/analytics/hourly').then(h => {
      const nd = $('#hourly'); if (nd) nd.innerHTML = svgBar(h.map(r => r.h.slice(0, 2)),
        [{ name: 'Units', data: h.map(r => r.units) },
         { name: 'Alarms', data: h.map(r => r.alarms), color: 'var(--bad)' }], { height: 200 });
    });
    get('/analytics/heatmap?days=21').then(hm => {
      const nd = $('#heatmap'); if (nd) nd.innerHTML = heatmap(hm);
    });
    get('/alerts?status=open&days=3').then(al => {
      const nd = $('#livealerts'); if (!nd) return;
      nd.innerHTML = al.length ? table([
        { label: t('laser.col.severity'), key: 'severity', render: a => sevBadge(a.severity) },
        { label: t('laser.col.machine'), key: 'machine_name' }, { label: t('laser.col.alert'), key: 'title' },
        { label: t('laser.col.age'), key: 'started_at', render: a => esc(ago(a.started_at)) },
        ...(can('ack') ? [{ label: '', key: 'x', render: a => `<button class="sm" onclick="event.stopPropagation();ackAlert(${a.id})">Ack</button>` }] : [])],
        al.slice(0, 12)) : '<div class="empty">✓ No open alerts</div>';
    });
  };
  await render();
  autoRefresh(render);
}

function kpi(label, value, sub, cls, spark) {
  const sp = spark && spark.some(x => x)
    ? `<div class="spark">${sparkline(spark, { w: 74, h: 26, flat: true, color: 'var(--accent)' })}</div>` : '';
  return `<div class="kpi ${cls || ''}"><div class="l">${esc(label)}</div><div class="v">${value}</div><div class="s">${sub || ''}</div>${sp}</div>`;
}

function machineCard(m) {
  const target = m.target ? Math.min(100, (m.units / m.target) * 100) : (m.utilization || 0);
  return `<div class="mc s-${esc(m.status)}" onclick="go('machine/${m.id}')">
    <div class="h"><span class="dot"></span><b>${esc(m.name)}</b><span class="st">${sic(STATUS_ICON[m.status] || "?")}${esc(tStatus(m.status))}</span></div>
    <div class="row"><span>Units today</span><b>${n0(m.units)}${m.target ? ' / ' + n0(m.target) : ''}</b></div>
    <div class="row"><span>Utilization</span><b>${pct(m.utilization)}</b></div>
    <div class="row"><span>Health</span><b style="color:${healthColor(m.health)}">${m.health === null ? '&mdash;' : Math.round(m.health)}</b></div>
    <div class="row"><span>${m.status === 'PRODUCING' ? 'Running' : 'Idle'}</span><b>${dur(m.idle_seconds)}</b></div>
    <div class="row"><span>Heartbeat</span><b>${esc(ago(m.last_heartbeat))}</b></div>
    <div class="design" title="${esc(m.current_design || '')}">${esc(m.current_design || 'no job')}</div>
    ${m.spark && m.spark.some(x => x) ? `<div class="cardspark">${sparkline(m.spark, { w: 224, h: 26, color: 'var(--accent)' })}</div>` : ''}
    <div class="bar"><i style="width:${Math.min(100, target)}%;background:${target >= 90 ? 'var(--ok)' : target >= 60 ? 'var(--accent)' : 'var(--warn)'}"></i></div>
  </div>`;
}
/* Status is never colour-alone: every status colour ships with a glyph + label.
   ~8% of men have a red-green colour deficiency; on a factory floor "running"
   vs "stopped" must survive that. */
const STATUS_ICON = {
  PRODUCING: '▶', IDLE: '⏸', STOPPED: '■', ALARM: '⚠',
  OFFLINE: '✖', MAINTENANCE: '⚒', DISABLED: '—', UNKNOWN: '?',
};
const SEV_ICON = { critical: '⛔', serious: '⚠', warning: '⚠', info: 'ℹ', ok: '✓' };
const sic = ch => `<i class="sic">${ch}</i>`;
function statusBadge(s) {
  s = s || 'UNKNOWN';
  const cls = s === 'PRODUCING' ? 'ok' : (s === 'OFFLINE' || s === 'STOPPED' || s === 'ALARM')
    ? 'critical' : s === 'IDLE' ? 'warning' : 'info';
  return `<span class="badge b-${cls}">${sic(STATUS_ICON[s] || '?')}${esc(tStatus(s))}</span>`;
}
function sevBadge(sv) {
  sv = (sv || 'info').toLowerCase();
  return `<span class="badge b-${esc(sv)}">${sic(SEV_ICON[sv] || 'ℹ')}${esc(tSev(sv))}</span>`;
}
const healthColor = h => h === null || h === undefined ? 'var(--dim)' : h >= 80 ? 'var(--ok)' : h >= 60 ? 'var(--warn)' : 'var(--bad)';
function go(h) { location.hash = '#/' + h; }

/* ------------------------------------------------------ 2. management */
async function pageManagement() {
  $('#title').textContent = t('laser.page.management');
  const days = +(localStorage.mgmtDays || 7);
  const [intel, trend, rank, cap] = await Promise.all([
    get('/intelligence?days=' + days), get('/analytics/trend?days=30'),
    get('/analytics/ranking?days=' + days), get('/analytics/capacity?days=' + days)]);
  const ov = CACHE.ov || await get('/overview'); const tot = ov.totals;
  $('#view').innerHTML = `
    <div class="toolbar">Period
      <select onchange="localStorage.mgmtDays=this.value;route()">
        ${[1, 7, 14, 30, 90].map(d => `<option value="${d}" ${d === days ? 'selected' : ''}>last ${d} day${d > 1 ? 's' : ''}</option>`).join('')}
      </select>
      <span class="right hint">Department view · ${esc(today())}</span></div>
    <div class="kpis">
      ${kpi(t('laser.kpi.units_today'), n0(tot.units), `forecast ${n0(intel.forecast_units)}`, 'accent', trend.map(r => r.units))}
      ${kpi(t('laser.kpi.on_track'), intel.on_track === null ? '&mdash;' : (intel.on_track ? 'YES' : 'AT RISK'), tot.target ? `target ${n0(tot.target)}` : 'no target set', intel.on_track === null ? '' : intel.on_track ? 'ok' : 'bad')}
      ${kpi('Utilization', pct(intel.department_utilization), `${days}-day cap. ${pct(cap.utilization_pct)}`, intel.department_utilization >= 70 ? 'ok' : 'warn', trend.map(r => r.utilization))}
      ${kpi(t('laser.kpi.lost_capacity'), n1(intel.lost_capacity_hours) + ' h', `${n0(intel.lost_units_estimate)} units est.`, 'bad')}
      ${kpi(t('laser.kpi.avg_oee'), pct(avg(rank.rows.map(r => r.oee))), 'availability × performance × quality', '', trend.map(r => r.oee))}
      ${kpi(t('laser.kpi.avg_health'), n1(avg(rank.rows.map(r => r.health))), 'all machines', '', trend.map(r => r.health))}
      ${kpi(t('laser.kpi.downtime'), n1(cap.down_hours) + ' h', `idle ${n1(cap.idle_hours)} h`, 'warn', trend.map(r => r.down_hours))}
      ${kpi(t('laser.kpi.open_alerts'), n0(tot.open_alerts), `${tot.critical_alerts} critical`, tot.critical_alerts ? 'bad' : 'ok', trend.map(r => r.alarms))}
    </div>

    <div class="grid2">
      <div class="panel"><h3>${esc(t('laser.panel.production_and_utilization_trend'))} <span class="sub">${esc(t('laser.sub.30_days'))}</span></h3><div class="body">
        ${svgLine([{name: 'Units', data: trend.map(r => r.units), area: true}], {labels: trend.map(r => r.d.slice(5)), height: 200})}
        ${svgLine([{name: 'Utilization %', data: trend.map(r => r.utilization)},
                   {name: 'OEE %', data: trend.map(r => r.oee)},
                   {name: 'Health', data: trend.map(r => r.health)}], {labels: trend.map(r => r.d.slice(5)), height: 190})}
      </div></div>
      <div class="panel"><h3>${esc(t('laser.panel.ranking'))} <span class="sub">last ${days} days</span></h3><div class="body">
        ${table([{label: t('laser.col.machine'), key: 'name'}, {label: t('laser.col.units'), key: 'units', num: true, render: r => n0(r.units)},
                 {label: t('laser.col.utilization'), key: 'utilization', num: true, render: r => pct(r.utilization)},
                 {label: t('laser.col.oee'), key: 'oee', num: true, render: r => pct(r.oee)},
                 {label: t('laser.col.health'), key: 'health', num: true, render: r => `<b style="color:${healthColor(r.health)}">${n0(r.health)}</b>`},
                 {label: t('laser.col.down_min'), key: 'down_minutes', num: true, render: r => n0(r.down_minutes)},
                 {label: t('laser.col.alarms'), key: 'alarms', num: true}],
          rank.rows, {onclick: r => `go('machine/${r.machine_id}')`})}</div></div>
    </div>

    <div class="grid3">
      <div class="panel"><h3>${esc(t('laser.panel.best_and_worst'))}</h3><div class="body">
        ${miniStat('Highest output', intel.top_producer)}
        ${miniStat('Lowest output', intel.lowest_producer)}
        ${miniStat('Highest downtime', intel.highest_downtime, 'down_minutes', 'min down')}
        ${intel.bottleneck ? `<div style="margin-top:10px"><div class="hint">Main bottleneck</div>
          <b>${esc(intel.bottleneck.name)}</b> — ${n1(intel.bottleneck.lost_hours)} h lost,
          ${n0(intel.bottleneck.lost_units_estimate)} units of capacity</div>` : ''}
      </div></div>
      <div class="panel"><h3>${esc(t('laser.panel.todays_loss'))}</h3><div class="body">
        ${svgBar(Object.keys(intel.todays_loss.buckets), [{name: 'Lost units', data: Object.values(intel.todays_loss.buckets)}], {height: 170})}
        <div class="hint" style="margin-top:6px">Total estimated loss: <b>${n0(intel.todays_loss.total_lost_units)}</b> units</div>
      </div></div>
      <div class="panel"><h3>${esc(t('laser.panel.capacity_split'))} <span class="sub">last ${days} days</span></h3><div class="body">
        ${svgDonut([
          { label: t('laser.col.running'), value: cap.busy_hours, color: 'var(--ok)' },
          { label: t('laser.status.IDLE'), value: cap.idle_hours, color: 'var(--warn)' },
          { label: t('laser.col.down'), value: cap.down_hours, color: 'var(--bad)' }],
          { center: pct(cap.utilization_pct), label: 'utilized' })}
        <div class="statline" style="margin-top:8px"><span>Units per machine-hour</span><b>${n1(cap.units_per_machine_hour)}</b></div>
        <div class="statline"><span>Unused capacity (units)</span><b>${n0(cap.unused_capacity_units)}</b></div>
        <div class="statline"><span>Capacity at 100%</span><b>${n0(cap.capacity_units_at_100pct)}</b></div>
      </div></div>
    </div>

    <div class="grid2">
      <div class="panel"><h3>${esc(t('laser.panel.output_ranking'))} <span class="sub">last ${days} days</span></h3><div class="body">
        ${svgBar(rank.rows.map(r => short(r.name)), [{ name: 'Units', data: rank.rows.map(r => r.units) }], { height: 240, rotate: true })}</div></div>
      <div class="panel"><h3>${esc(t('laser.panel.production_rhythm'))} <span class="sub">${esc(t('laser.sub.avg_units_by_weekday_and_hour'))}</span></h3>
        <div class="body" id="mgmtheat">Loading…</div></div>
    </div>`;
  get('/analytics/heatmap?days=28').then(hm => { const nd = $('#mgmtheat'); if (nd) nd.innerHTML = heatmap(hm); });
}
const avg = a => { const v = a.filter(x => x !== null && x !== undefined && !isNaN(x)); return v.length ? v.reduce((x, y) => x + y, 0) / v.length : null; };
function miniStat(label, row, key, unit) {
  if (!row) return '';
  return `<div style="margin-bottom:8px"><div class="hint">${esc(label)}</div>
    <b>${esc(row.name)}</b> — ${n0(row[key || 'units'])} ${esc(unit || 'units')}</div>`;
}

/* ------------------------------------------------------ 3. production */
async function pageProduction() {
  $('#title').textContent = t('laser.page.production');
  const render = async () => {
    const [ov, hourly, shifts, loss] = await Promise.all([
      get('/overview'), get('/analytics/hourly'), get('/analytics/shifts?days=7'), get('/analytics/loss')]);
    const tot = ov.totals;
    const idle = ov.machines.filter(m => ['IDLE', 'STOPPED', 'OFFLINE'].includes(m.status));
    const shiftAgg = {};
    shifts.forEach(s => { const a = shiftAgg[s.shift || '-'] || (shiftAgg[s.shift || '-'] = {units: 0, down: 0}); a.units += s.units || 0; a.down += s.down_hours || 0; });
    paint(`
      <div class="kpis">
        ${kpi(t('laser.kpi.units_today'), n0(tot.units), `${n0(tot.jobs)} jobs`, 'accent')}
        ${kpi(t('laser.kpi.target'), n0(tot.target) || '&mdash;', pct(tot.target_pct), tot.target_pct >= 95 ? 'ok' : 'warn')}
        ${kpi('Forecast', n0(tot.forecast_units), 'end of day')}
        ${kpi(t('laser.kpi.producing_now'), `${tot.machines_producing}/${tot.machines_total}`, 'machines', 'ok')}
        ${kpi(t('laser.kpi.idle_stopped'), n0(idle.length), 'machines not producing', idle.length ? 'warn' : 'ok')}
        ${kpi(t('laser.kpi.lost_units'), n0(loss.total_lost_units), 'idle + stopped + offline', 'bad')}
      </div>
      <div class="panel"><h3>${esc(t('laser.panel.live_state'))}</h3><div class="body"><div class="mgrid">
        ${ov.machines.map(machineCard).join('')}</div></div></div>
      <div class="grid2">
        <div class="panel"><h3>${esc(t('laser.panel.hourly_output'))} <span class="sub">${esc(t('laser.sub.today'))}</span></h3><div class="body">
          ${svgBar(hourly.map(r => r.h.slice(0, 2)), [
            {name: 'Units', data: hourly.map(r => r.units)}], {height: 200})}
          ${svgBar(hourly.map(r => r.h.slice(0, 2)), [
            {name: 'Busy h', data: hourly.map(r => r.busy_hours), color: 'var(--ok)'},
            {name: 'Idle h', data: hourly.map(r => r.idle_hours), color: 'var(--warn)'},
            {name: 'Down h', data: hourly.map(r => r.down_hours), color: 'var(--bad)'}], {height: 190, stacked: true})}
        </div></div>
        <div class="panel"><h3>${esc(t('laser.panel.shift_performance'))} <span class="sub">${esc(t('laser.sub.last_7_days'))}</span></h3><div class="body">
          ${svgBar(Object.keys(shiftAgg), [{name: 'Units', data: Object.values(shiftAgg).map(a => a.units)}], {height: 170})}
          ${table([{label: t('laser.col.date'), key: 'production_date'}, {label: t('laser.col.shift'), key: 'shift'},
                   {label: t('laser.col.units'), key: 'units', num: true, render: r => n0(r.units)},
                   {label: t('laser.col.jobs'), key: 'jobs', num: true},
                   {label: t('laser.col.busy_h'), key: 'busy_hours', num: true, render: r => n1(r.busy_hours)},
                   {label: t('laser.col.idle_h'), key: 'idle_hours', num: true, render: r => n1(r.idle_hours)},
                   {label: t('laser.col.alarms'), key: 'alarms', num: true}], shifts.slice().reverse())}
        </div></div>
      </div>
      <div class="panel"><h3>${esc(t('laser.panel.loss_by_machine'))} <span class="sub">${esc(t('laser.sub.today_estimated_units'))}</span></h3><div class="body">
        ${table([{label: t('laser.col.machine'), key: 'name'}, { label: t('laser.col.units_made'), key: 'units', num: true, render: r => n0(r.units)},
                 { label: t('laser.col.lost_idle'), key: 'lost_idle', num: true}, { label: t('laser.col.lost_stopped'), key: 'lost_stopped', num: true},
                 { label: t('laser.col.lost_offline'), key: 'lost_offline', num: true}, {label: t('laser.col.alarms'), key: 'alarms', num: true}],
          loss.by_machine, {onclick: r => `go('machine/${r.machine_id}')`})}</div></div>`);
  };
  await render();
  autoRefresh(render);
}

/* ----------------------------------------------------- 4. maintenance */
async function pageMaintenance() {
  $('#title').textContent = t('laser.page.maintenance');
  const days = +(localStorage.mntDays || 30);
  const [al, dn, rank, machines, optics] = await Promise.all([
    get('/analytics/alarms?days=' + days), get('/analytics/downtime?days=' + days),
    get('/analytics/ranking?days=' + days + '&metric=health'), get('/machines'),
    get('/analytics/optics-drift').catch(() => null)]);
  const risks = await Promise.all(machines.filter(m => m.enabled).map(m =>
    get('/analytics/forecast?machine_id=' + m.id).then(f => ({m, f})).catch(() => null)));
  const rows = risks.filter(Boolean).map(({m, f}) => ({
    machine_id: m.id, name: m.name, model: m.model || m.detected_model,
    risk: f.maintenance.level, score: f.maintenance.risk,
    health: f.health.current, trend: f.health.slope_per_day, in7: f.health.in_7_days,
    crit: f.maintenance.critical_alarms_14d, drift: f.maintenance.head_drift_pct,
    temp: f.maintenance.temp_slope, reasons: f.maintenance.reasons.join(' · '),
    downtime: f.downtime.expected_downtime_minutes,
  })).sort((a, b) => b.score - a.score);
  $('#view').innerHTML = `
    <div class="toolbar">Period <select onchange="localStorage.mntDays=this.value;route()">
      ${[7, 14, 30, 90].map(d => `<option value="${d}" ${d === days ? 'selected' : ''}>last ${d} days</option>`).join('')}</select>
      <span class="right"><button onclick="downloadReport('maintenance','pdf',${days})">Export PDF</button>
      <button onclick="downloadReport('maintenance','xlsx',${days})">Export Excel</button></span></div>
    <div class="kpis">
      ${kpi(t('laser.kpi.high_risk'), n0(rows.filter(r => r.risk === 'high').length), 'need attention now', 'bad')}
      ${kpi(t('laser.kpi.medium_risk'), n0(rows.filter(r => r.risk === 'medium').length), 'monitor')}
      ${kpi(t('laser.kpi.avg_health'), n1(avg(rows.map(r => r.health))), 'all machines')}
      ${kpi(t('laser.kpi.alarms'), n0(al.by_machine.reduce((a, r) => a + r.n, 0)), `${days} days`)}
      ${kpi(t('laser.kpi.critical_alarms'), n0(al.by_machine.reduce((a, r) => a + (r.critical || 0), 0)), `${days} days`, 'bad')}
      ${kpi(t('laser.kpi.downtime'), n1(dn.by_kind.reduce((a, r) => a + (r.hours || 0), 0)) + ' h', `${days} days`, 'warn')}
    </div>
    <div class="panel"><h3>${esc(t('laser.panel.predictive_risk'))} <span class="sub">${esc(t('laser.panel.predictive_sub'))}</span></h3><div class="body">
      ${table([{label: t('laser.col.machine'), key: 'name'}, {label: t('laser.col.model'), key: 'model'},
               { label: t('laser.col.risk'), key: 'risk', render: r => `<span class="badge b-${r.risk === 'high' ? 'critical' : r.risk === 'medium' ? 'warning' : 'ok'}">${esc(r.risk)}</span>`},
               {label: t('laser.col.score'), key: 'score', num: true},
               {label: t('laser.col.health'), key: 'health', num: true, render: r => `<b style="color:${healthColor(r.health)}">${n0(r.health)}</b>`},
               { label: t('laser.col.trend_day'), key: 'trend', num: true, render: r => n1(r.trend)},
               { label: t('laser.col.in_7_days'), key: 'in7', num: true, render: r => n0(r.in7)},
               { label: t('laser.col.crit_alarms_14d'), key: 'crit', num: true},
               { label: t('laser.col.head_drift'), key: 'drift', num: true, render: r => n1(r.drift)},
               { label: t('laser.col.exp_downtime_day'), key: 'downtime', num: true, render: r => n0(r.downtime) + ' min'},
               { label: t('laser.col.findings'), key: 'reasons'}],
        rows, {onclick: r => `go('machine/${r.machine_id}')`})}</div></div>
    <div class="grid2">
      <div class="panel"><h3>${esc(t('laser.panel.frequent_alarms'))}</h3><div class="body">
        ${svgBar(al.top.slice(0, 12).map(r => (r.description || '').slice(0, 26)),
          [{name: 'Occurrences', data: al.top.slice(0, 12).map(r => r.n)}], {height: 250, rotate: true})}
        ${table([{ label: t('laser.status.ALARM'), key: 'description'}, {label: t('laser.col.category'), key: 'category'},
                 {label: t('laser.col.severity'), key: 'severity', render: r => sevBadge(r.severity)},
                 {label: t('laser.col.code'), key: 'error_code', num: true}, {label: t('laser.col.count'), key: 'n', num: true},
                 {label: t('laser.col.machines'), key: 'machines', num: true}, {label: t('laser.col.last_seen'), key: 'last_seen'}], al.top)}
      </div></div>
      <div class="panel"><h3>${esc(t('laser.panel.downtime_analysis'))}</h3><div class="body">
        ${svgBar(dn.by_machine.map(r => r.name.replace('Laser Machine ', 'LM')),
          [{name: 'Hours', data: dn.by_machine.map(r => r.hours), color: 'var(--bad)'}], {height: 210, rotate: true})}
        ${table([{label: t('laser.col.category'), key: 'kind'}, {label: t('laser.col.events'), key: 'events', num: true},
                 {label: t('laser.col.hours'), key: 'hours', num: true, render: r => n1(r.hours)},
                 {label: t('laser.col.avg_min'), key: 'avg_minutes', num: true, render: r => n1(r.avg_minutes)},
                 {label: t('laser.col.max_min'), key: 'max_minutes', num: true, render: r => n1(r.max_minutes)}], dn.by_kind)}
        <h4 style="margin:14px 0 6px;font-size:12px">${esc(t('laser.panel.longest_stoppages'))}</h4>
        ${table([{label: t('laser.col.machine'), key: 'name'}, {label: t('laser.col.start'), key: 'start_time'},
                 {label: t('laser.col.kind'), key: 'kind'}, {label: t('laser.col.minutes'), key: 'minutes', num: true, render: r => n0(r.minutes)}],
          dn.longest)}
      </div></div>
    </div>
    ${opticsPanel(optics)}
    <div class="panel"><h3>${esc(t('laser.panel.alarm_trend'))}</h3><div class="body">
      ${svgLine([{name: 'All alarms', data: al.daily.map(r => r.n), area: true},
                 {name: 'Critical', data: al.daily.map(r => r.critical), color: 'var(--bad)'}],
        {labels: al.daily.map(r => r.d.slice(5)), height: 190})}</div></div>`;
}

/* The laser recipe (config.db) compared machine-to-machine within the same model.
   markSpeed/jumpSpeed set the physical marking ceiling, so a machine configured
   below its peers is permanently slower and no production metric explains why. */
function opticsPanel(d) {
  if (!d || !d.findings) return '';
  const crit = d.findings.filter(f => f.severity === 'critical');
  if (!d.by_machine.length) {
    return `<div class="panel"><h3>${esc(t('laser.panel.laser_optics_configuration'))}
      <span class="sub">${d.machines_with_optics} machines · ${d.presets} presets · ${d.parameters} parameters</span></h3>
      <div class="body"><div class="empty">✓ Every machine matches the standard for its model</div></div></div>`;
  }
  return `<div class="panel"><h3>${esc(t('laser.panel.laser_optics_configuration_drift'))}
      <span class="sub">${d.machines_with_optics} machines · ${d.presets} presets · ${d.parameters} parameters compared within each model</span>
      <span class="right">${can('config') ? `<button onclick="refreshOptics()">Re-read from machines</button>` : ''}
        <button onclick="downloadReport('optics','xlsx')">Excel</button>
        <button onclick="downloadReport('optics','pdf')">PDF</button></span></h3>
    <div class="body">
      <div class="hint" style="margin-bottom:10px">The reference is the fleet itself: for each model, preset and
        parameter, the value most machines agree on is the standard. A machine differing from it is shown here,
        alongside how it actually performs — context for a decision, not a claim of cause.</div>
      ${table([
        {label: t('laser.col.machine'), key: 'machine'},
        {label: t('laser.col.model'), key: 'model'},
        { label: t('laser.col.count'), key: 'total', num: true},
        {label: t('laser.col.critical'), key: 'critical', num: true,
         render: b => b.critical ? `<b style="color:var(--bad)">${b.critical}</b>` : '0'},
        {label: t('laser.col.units_hour'), key: 'units_per_hour', num: true, render: b => n1(b.units_per_hour)},
        { label: t('laser.col.model_median'), key: 'model_median_uph', num: true, render: b => n1(b.model_median_uph)},
        { label: t('laser.col.vs_model'), key: 'vs_model_pct', num: true, render: b => b.vs_model_pct === null
          ? '&mdash;' : `<b style="color:${b.vs_model_pct < -8 ? 'var(--bad)' : b.vs_model_pct < 0 ? 'var(--warn)' : 'var(--ok)'}">${b.vs_model_pct > 0 ? '+' : ''}${n1(b.vs_model_pct)}%</b>`},
      ], d.by_machine, {onclick: b => `go('machine/${b.machine_id}')`})}

      <h4 style="margin:14px 0 6px;font-size:12px;color:var(--muted)">
        Speed &amp; power deviations (${crit.length})</h4>
      ${table([
        {label: t('laser.col.machine'), key: 'machine'},
        {label: t('laser.col.model'), key: 'model'},
        { label: t('laser.col.preset'), key: 'preset'},
        { label: t('laser.col.parameter'), key: 'param'},
        { label: t('laser.col.value'), key: 'value'},
        { label: t('laser.col.fleet_standard'), key: 'standard'},
        { label: t('laser.col.peers_agree'), key: 'agree', render: f => `${f.agree}/${f.peers}`},
        { label: t('laser.col.delta'), key: 'delta_pct', num: true,
         render: f => f.delta_pct === null ? '&mdash;' : `${f.delta_pct > 0 ? '+' : ''}${n1(f.delta_pct)}%`},
        {label: t('laser.col.area'), key: 'area'},
      ], crit.slice(0, 60), {empty: 'No speed or power deviations'})}
      ${crit.length > 60 ? `<div class="hint" style="margin-top:6px">Showing 60 of ${crit.length} — full list in the Excel export.</div>` : ''}
    </div></div>`;
}

async function refreshOptics() {
  busy(true);
  try {
    const r = await post('/analytics/optics-refresh');
    toast(`Optics re-read from ${r.refreshed} machines`, 'ok');
    route();
  } catch (e) { toast(e.message, 'bad'); }
  busy(false);
}

/* ------------------------------------------------------- 5. technical */
async function pageTechnical() {
  $('#title').textContent = t('laser.page.technical');
  const render = async () => {
    const [sys, machines] = await Promise.all([get('/system'), get('/machines')]);
    const ing = sys.ingestion_last_24h;
    paint(`
      <div class="kpis">
        ${kpi('Machines online', `${machines.filter(m => m.online).length}/${machines.filter(m => m.enabled).length}`, 'heartbeat OK',
          machines.filter(m => m.enabled && !m.online).length ? 'warn' : 'ok')}
        ${kpi(t('laser.kpi.database_size'), n1(sys.database.size_mb) + ' MB', esc(sys.database.path.split(/[\\/]/).pop()))}
        ${kpi(t('laser.kpi.production_rows'), n0(sys.database.counts.production), 'job records')}
        ${kpi(t('laser.kpi.alarm_rows'), n0(sys.database.counts.machine_alarms), 'from machine error.db')}
        ${kpi(t('laser.kpi.last_sync'), esc(ago(sys.last_sync)), esc(sys.last_sync || ''))}
        ${kpi(t('laser.kpi.failing_machines'), n0(sys.failing_machines.length), 'last 24 h', sys.failing_machines.length ? 'bad' : 'ok')}
        ${kpi(t('laser.kpi.last_backup'), esc((sys.backup && sys.backup[0] ? sys.backup[0].value : 'never').slice(0, 16)), 'nightly 02:15')}
      </div>
      <div class="panel"><h3>${esc(t('laser.panel.connectivity_and_synchronisation'))}
        <span class="right">${can('settings') ? '<button onclick="doBackup()">Backup now</button>' : ''}
        <button onclick="downloadReport('connectivity','xlsx',7)">Export</button></span></h3><div class="body">
        ${table([
          {label: t('laser.col.machine'), key: 'name'}, { label: t('laser.col.id'), key: 'id', num: true},
          {label: t('laser.col.host'), key: 'host'}, {label: t('laser.col.port'), key: 'port', num: true},
          {label: t('laser.col.method'), key: 'connection_method'},
          {label: t('laser.col.link'), key: 'connectivity', render: r => `<span class="badge b-${r.connectivity === 'CONNECTED' ? 'ok' : r.connectivity === 'SLOW' ? 'warning' : 'critical'}">${esc(r.connectivity || '-')}</span>`},
          {label: t('laser.col.status'), key: 'status'},
          {label: t('laser.col.latency'), key: 'latency_ms', num: true, render: r => r.latency_ms ? n0(r.latency_ms) + ' ms' : '&mdash;'},
          {label: t('laser.col.last_heartbeat'), key: 'last_heartbeat_ok', render: r => esc(ago(r.last_heartbeat_ok))},
          {label: t('laser.col.last_sync'), key: 'last_sync_ok', render: r => esc(ago(r.last_sync_ok))},
          { label: t('laser.col.src_id'), key: 'last_source_id', num: true},
          {label: t('laser.col.source_mtime'), key: 'source_mtime'},
          {label: t('laser.col.fails'), key: 'consecutive_fail', num: true},
          {label: t('laser.col.last_error'), key: 'last_error', render: r => `<span class="mono" title="${esc(r.last_error || '')}">${esc((r.last_error || '').slice(0, 60))}</span>`},
        ], machines, {onclick: r => `go('machine/${r.id}')`})}</div></div>
      <div class="grid2">
        <div class="panel"><h3>${esc(t('laser.panel.data_ingestion'))} <span class="sub">${esc(t('laser.panel.ingestion_sub'))}</span></h3><div class="body">
          ${svgBar(ing.map(r => r.h.slice(11, 13)), [
            {name: 'Rows', data: ing.map(r => r.rows)},
            {name: 'Failures', data: ing.map(r => r.failures), color: 'var(--bad)'}], {height: 200})}
          <div class="legend">Runs: ${ing.reduce((a, r) => a + r.runs, 0)} · Rows: ${n0(ing.reduce((a, r) => a + (r.rows || 0), 0))}</div>
        </div></div>
        <div class="panel"><h3>${esc(t('laser.panel.db_contents'))}</h3><div class="body">
          ${table([{label: t('laser.col.table'), key: 'k'}, {label: t('laser.col.rows'), key: 'v', num: true, render: r => n0(r.v)}],
            Object.entries(sys.database.counts).map(([k, v]) => ({k, v})))}
          <div class="hint" style="margin-top:8px">Server time ${esc(sys.server_time)}</div>
        </div></div>
      </div>
      <div class="panel"><h3>${esc(t('laser.panel.public_access'))}</h3><div class="body" id="puburl">${esc(t('laser.msg.loading'))}</div></div>
      <div class="panel"><h3>${esc(t('laser.panel.failing_24h'))}</h3><div class="body">
        ${table([{label: t('laser.col.machine'), key: 'name'}, { label: t('laser.col.failures'), key: 'n', num: true},
                 { label: t('laser.col.last_failure'), key: 'last'}], sys.failing_machines, {empty: 'No connection failures'})}</div></div>`);
  };
  await render();
  showPublicUrl();
  autoRefresh(render);
}

/* The public hostname is disposable and changes on its own; this LAN page is the
   stable place to read whatever it currently is. */
async function showPublicUrl() {
  const el2 = $('#puburl');
  if (!el2) return;
  try {
    const p = await get('/system/public-url');
    el2.innerHTML = p.url
      ? `<div style="font-size:15px"><a href="${esc(p.url)}" target="_blank" rel="noopener">${esc(p.url)}</a></div>
         <div class="hint" style="margin-top:6px">Checked ${esc(p.checked || '-')} — this address changes when the
         tunnel restarts; this page always shows the current one. The LAN address
         <b>${esc(location.origin)}</b> never changes.</div>`
      : `<div class="empty">No public URL — the tunnel keeper has not registered one yet</div>`;
  } catch (e) { el2.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}

async function doBackup() { busy(true); try { const r = await post('/system/backup'); toast('Backup written: ' + r.file, 'ok'); } catch (e) { toast(e.message, 'bad'); } busy(false); }

/* -------------------------------------------------------- 6. analytics */
async function pageAnalytics() {
  $('#title').textContent = t('laser.page.analytics');
  const days = +(localStorage.anDays || 30);
  const tab = localStorage.anTab || 'compare';
  const tabs = {compare: 'Machine comparison', designs: 'Designs & styles', operators: 'Operators',
                shifts: 'Shifts', downtime: 'Downtime', alarms: 'Alarms', forecast: 'Forecast'};
  $('#view').innerHTML = `
    <div class="toolbar">Period <select onchange="localStorage.anDays=this.value;route()">
      ${[7, 14, 30, 60, 90, 180, 365].map(d => `<option value="${d}" ${d === days ? 'selected' : ''}>last ${d} days</option>`).join('')}</select></div>
    <div class="tabs">${Object.entries(tabs).map(([k, v]) => `<a href="#" onclick="localStorage.anTab='${k}';route();return false" class="${k === tab ? 'on' : ''}">${v}</a>`).join('')}</div>
    <div id="tabbody"><div class="empty">Loading…</div></div>`;
  const body = $('#tabbody');
  if (tab === 'compare') {
    const rank = await get('/analytics/ranking?days=' + days);
    const r = rank.rows;
    body.innerHTML = `
      <div class="grid2">
        <div class="panel"><h3>${esc(t('laser.panel.units_produced'))}</h3><div class="body">${svgBar(r.map(x => short(x.name)), [{name: 'Units', data: r.map(x => x.units)}], {height: 230, rotate: true})}</div></div>
        <div class="panel"><h3>${esc(t('laser.panel.utilization_vs_oee'))}</h3><div class="body">${svgBar(r.map(x => short(x.name)), [
          {name: 'Utilization %', data: r.map(x => x.utilization)}, {name: 'OEE %', data: r.map(x => x.oee), color: 'var(--maint)'}], {height: 230, rotate: true})}</div></div>
        <div class="panel"><h3>${esc(t('laser.panel.downtime_and_idle_minutes'))}</h3><div class="body">${svgBar(r.map(x => short(x.name)), [
          {name: 'Down', data: r.map(x => x.down_minutes), color: 'var(--bad)'},
          {name: 'Idle', data: r.map(x => x.idle_minutes), color: 'var(--warn)'}], {height: 230, rotate: true, stacked: true})}</div></div>
        <div class="panel"><h3>${esc(t('laser.panel.throughput_units_per_running_hour'))}</h3><div class="body">${svgBar(r.map(x => short(x.name)), [{name: 'Units/h', data: r.map(x => x.uph), color: 'var(--ok)'}], {height: 230, rotate: true})}</div></div>
      </div>
      <div class="panel"><h3>${esc(t('laser.panel.comparison'))}</h3><div class="body">${table([
        {label: t('laser.col.machine'), key: 'name'}, {label: t('laser.col.units'), key: 'units', num: true, render: x => n0(x.units)},
        {label: t('laser.col.jobs'), key: 'jobs', num: true, render: x => n0(x.jobs)},
        {label: t('laser.col.utilization'), key: 'utilization', num: true, render: x => pct(x.utilization)},
        {label: t('laser.col.performance'), key: 'performance', num: true, render: x => pct(x.performance)},
        {label: t('laser.col.oee'), key: 'oee', num: true, render: x => pct(x.oee)},
        {label: t('laser.col.health'), key: 'health', num: true, render: x => `<b style="color:${healthColor(x.health)}">${n0(x.health)}</b>`},
        {label: t('laser.col.units_hour'), key: 'uph', num: true, render: x => n1(x.uph)},
        {label: t('laser.col.laser_duty'), key: 'laser_duty', num: true, render: x => n1(x.laser_duty)},
        {label: t('laser.col.down_min'), key: 'down_minutes', num: true, render: x => n0(x.down_minutes)},
        {label: t('laser.col.idle_min'), key: 'idle_minutes', num: true, render: x => n0(x.idle_minutes)},
        {label: t('laser.col.alarms'), key: 'alarms', num: true}], r, {onclick: x => `go('machine/${x.machine_id}')`})}</div></div>`;
  } else if (tab === 'designs') {
    const d = await get('/analytics/designs?days=' + days + '&limit=60');
    body.innerHTML = `<div class="panel"><h3>${esc(t('laser.panel.design_style_performance'))} <span class="sub">${esc(t('laser.sub.cycle_time_is_seconds_per_unit'))}</span></h3><div class="body">
      ${svgBar(d.slice(0, 15).map(x => short(x.design, 24)), [{name: 'Units', data: d.slice(0, 15).map(x => x.units)}], {height: 240, rotate: true})}
      ${table([{label: t('laser.col.design'), key: 'design'}, {label: t('laser.col.style'), key: 'style'}, {label: t('laser.col.size'), key: 'size'},
               { label: t('laser.col.color_2'), key: 'color'}, {label: t('laser.col.jobs'), key: 'jobs', num: true},
               {label: t('laser.col.units'), key: 'units', num: true, render: x => n0(x.units)},
               { label: t('laser.col.avg_s_unit'), key: 'sec_per_unit', num: true, render: x => n1(x.sec_per_unit)},
               { label: t('laser.col.best_s_unit'), key: 'best_sec_per_unit', num: true, render: x => n1(x.best_sec_per_unit)},
               { label: t('laser.col.gap'), key: 'gap', num: true, render: x => x.best_sec_per_unit ? pct((x.sec_per_unit / x.best_sec_per_unit - 1) * 100) : '&mdash;'},
               {label: t('laser.col.laser_duty'), key: 'laser_duty', num: true, render: x => n1(x.laser_duty)},
               {label: t('laser.col.machines'), key: 'machines', num: true},
               {label: t('laser.col.hours'), key: 'hours', num: true, render: x => n1(x.hours)}], d)}
      <div class="hint" style="margin-top:8px">“Gap %” is how far the average cycle time sits above the best observed cycle time for the same design — the recoverable time.</div>
    </div></div>`;
  } else if (tab === 'operators') {
    const o = await get('/analytics/operators?days=' + days);
    body.innerHTML = `<div class="panel"><h3>${esc(t('laser.panel.operator_user_performance'))} <span class="sub">${esc(t('laser.sub.from_the_machine_login_recorded_on_each_job'))}</span></h3><div class="body">
      ${svgBar(o.slice(0, 15).map(x => short(x.operator || '-', 18)), [{name: 'Units', data: o.slice(0, 15).map(x => x.units)}], {height: 220, rotate: true})}
      ${table([{label: t('laser.col.operator'), key: 'operator'}, {label: t('laser.col.jobs'), key: 'jobs', num: true, render: x => n0(x.jobs)},
               {label: t('laser.col.units'), key: 'units', num: true, render: x => n0(x.units)},
               {label: t('laser.col.hours'), key: 'hours', num: true, render: x => n1(x.hours)},
               {label: t('laser.col.units_hour'), key: 'units_per_hour', num: true, render: x => n1(x.units_per_hour)},
               {label: t('laser.col.laser_duty'), key: 'laser_duty', num: true, render: x => n1(x.laser_duty)},
               { label: t('laser.col.jobs_w_alarm'), key: 'jobs_with_alarm', num: true},
               {label: t('laser.col.machines'), key: 'machines', num: true}], o)}</div></div>`;
  } else if (tab === 'shifts') {
    const s = await get('/analytics/shifts?days=' + days);
    const byShift = {}, byDate = {};
    s.forEach(r => {
      (byShift[r.shift || '-'] = byShift[r.shift || '-'] || {units: 0, down: 0, idle: 0, alarms: 0});
      byShift[r.shift || '-'].units += r.units || 0; byShift[r.shift || '-'].down += r.down_hours || 0;
      byShift[r.shift || '-'].idle += r.idle_hours || 0; byShift[r.shift || '-'].alarms += r.alarms || 0;
      byDate[r.production_date] = byDate[r.production_date] || {};
      byDate[r.production_date][r.shift || '-'] = r.units || 0;
    });
    const dates = Object.keys(byDate).sort(), names = Object.keys(byShift).sort();
    body.innerHTML = `<div class="grid2">
      <div class="panel"><h3>${esc(t('laser.panel.units_by_shift'))}</h3><div class="body">${svgBar(names, [{name: 'Units', data: names.map(k => byShift[k].units)}], {height: 210})}</div></div>
      <div class="panel"><h3>${esc(t('laser.panel.downtime_and_idle_hours_by_shift'))}</h3><div class="body">${svgBar(names, [
        {name: 'Down h', data: names.map(k => byShift[k].down), color: 'var(--bad)'},
        {name: 'Idle h', data: names.map(k => byShift[k].idle), color: 'var(--warn)'}], {height: 210})}</div></div></div>
      <div class="panel"><h3>${esc(t('laser.panel.daily_shift_output'))}</h3><div class="body">${svgBar(dates.map(d => d.slice(5)),
        names.map((nm, i) => ({name: 'Shift ' + nm, data: dates.map(d => byDate[d][nm] || 0), color: PALETTE[i]})), {height: 230, stacked: true})}
        ${table([{label: t('laser.col.date'), key: 'production_date'}, {label: t('laser.col.shift'), key: 'shift'},
                 {label: t('laser.col.units'), key: 'units', num: true, render: r => n0(r.units)}, {label: t('laser.col.jobs'), key: 'jobs', num: true},
                 {label: t('laser.col.busy_h'), key: 'busy_hours', num: true, render: r => n1(r.busy_hours)},
                 {label: t('laser.col.idle_h'), key: 'idle_hours', num: true, render: r => n1(r.idle_hours)},
                 {label: t('laser.col.down_h'), key: 'down_hours', num: true, render: r => n1(r.down_hours)},
                 {label: t('laser.col.alarms'), key: 'alarms', num: true}], s.slice().reverse())}</div></div>`;
  } else if (tab === 'downtime') {
    const d = await get('/analytics/downtime?days=' + days);
    body.innerHTML = `<div class="grid2">
      <div class="panel"><h3>${esc(t('laser.panel.by_category'))}</h3><div class="body">${svgBar(d.by_kind.map(x => x.kind), [{name: 'Hours', data: d.by_kind.map(x => x.hours), color: 'var(--bad)'}], {height: 200})}
        ${table([{label: t('laser.col.kind'), key: 'kind'}, {label: t('laser.col.events'), key: 'events', num: true},
                 {label: t('laser.col.hours'), key: 'hours', num: true, render: r => n1(r.hours)},
                 {label: t('laser.col.avg_min'), key: 'avg_minutes', num: true, render: r => n1(r.avg_minutes)}], d.by_kind)}</div></div>
      <div class="panel"><h3>${esc(t('laser.panel.by_machine'))}</h3><div class="body">${svgBar(d.by_machine.map(x => short(x.name)), [{name: 'Hours', data: d.by_machine.map(x => x.hours), color: 'var(--warn)'}], {height: 200, rotate: true})}
        ${table([{label: t('laser.col.machine'), key: 'name'}, {label: t('laser.col.events'), key: 'events', num: true},
                 {label: t('laser.col.hours'), key: 'hours', num: true, render: r => n1(r.hours)},
                 {label: t('laser.col.avg_min'), key: 'avg_minutes', num: true, render: r => n1(r.avg_minutes)}], d.by_machine)}</div></div></div>
      <div class="panel"><h3>${esc(t('laser.panel.longest_stoppages'))}</h3><div class="body">${table([
        {label: t('laser.col.machine'), key: 'name'}, {label: t('laser.col.start'), key: 'start_time'}, {label: t('laser.col.end'), key: 'end_time'},
        {label: t('laser.col.kind'), key: 'kind'}, {label: t('laser.col.minutes'), key: 'minutes', num: true, render: r => n0(r.minutes)},
        {label: t('laser.col.reason'), key: 'reason'}], d.longest)}</div></div>`;
  } else if (tab === 'alarms') {
    const a = await get('/analytics/alarms?days=' + days);
    body.innerHTML = `<div class="panel"><h3>${esc(t('laser.panel.alarm_trend'))}</h3><div class="body">${svgLine([
      {name: 'All', data: a.daily.map(r => r.n), area: true}, {name: 'Critical', data: a.daily.map(r => r.critical), color: 'var(--bad)'}],
      {labels: a.daily.map(r => r.d.slice(5)), height: 200})}</div></div>
      <div class="grid2">
        <div class="panel"><h3>${esc(t('laser.panel.top_alarms'))}</h3><div class="body">${table([
          {label: t('laser.col.description'), key: 'description'}, {label: t('laser.col.category'), key: 'category'},
          {label: t('laser.col.severity'), key: 'severity', render: r => sevBadge(r.severity)},
          {label: t('laser.col.code'), key: 'error_code', num: true}, {label: t('laser.col.count'), key: 'n', num: true},
          {label: t('laser.col.machines'), key: 'machines', num: true}, {label: t('laser.col.last_seen'), key: 'last_seen'}], a.top)}</div></div>
        <div class="panel"><h3>${esc(t('laser.panel.by_machine'))}</h3><div class="body">${svgBar(a.by_machine.map(x => short(x.name)), [
          {name: 'Warnings', data: a.by_machine.map(x => x.warning), color: 'var(--warn)'},
          {name: 'Critical', data: a.by_machine.map(x => x.critical), color: 'var(--bad)'}], {height: 220, rotate: true, stacked: true})}
          ${table([{label: t('laser.col.machine'), key: 'name'}, {label: t('laser.col.total'), key: 'n', num: true},
                   {label: t('laser.col.critical'), key: 'critical', num: true}, {label: t('laser.col.warning'), key: 'warning', num: true}], a.by_machine)}</div></div>
      </div>`;
  } else if (tab === 'forecast') {
    const f = await get('/analytics/forecast');
    const per = f.department.filter(r => r.machine_id);
    body.innerHTML = `<div class="panel"><h3>${esc(t('laser.panel.forecast'))}</h3><div class="body">
      ${svgBar(per.map(r => short(r.name)), [{name: 'Forecast units', data: per.map(r => r.value)}], {height: 230, rotate: true})}
      ${table([{label: t('laser.col.machine'), key: 'name'}, {label: t('laser.col.metric'), key: 'metric'},
               { label: t('laser.tab.forecast'), key: 'value', num: true, render: r => n0(r.value)},
               { label: t('laser.col.low'), key: 'low', num: true, render: r => n0(r.low)},
               { label: t('laser.col.high'), key: 'high', num: true, render: r => n0(r.high)},
               {label: t('laser.col.method'), key: 'method'}, { label: t('laser.col.calculated'), key: 'calculated_at'}], f.department)}
      <div class="hint" style="margin-top:8px">Forecasts blend the machine's own recent hourly rate with its learned hour-of-week baseline. Machines with fewer than three weeks of history use the recent rate only.</div>
    </div></div>
    <div class="panel"><h3>${esc(t('laser.panel.capacity_outlook'))} <span class="sub">${esc(t('laser.sub.last_7_days'))}</span></h3><div class="body">
      ${table([{label: t('laser.col.metric'), key: 'k'}, {label: t('laser.col.value'), key: 'v', num: true, render: r => n1(r.v)}],
        Object.entries(f.capacity).map(([k, v]) => ({k: k.replace(/_/g, ' '), v})))}</div></div>`;
  }
}
const short = (s, n) => String(s || '').replace('Laser Machine ', 'LM').slice(0, n || 14);

/* --------------------------------------------------------- 7. history */
async function pageHistory(args) {
  $('#title').textContent = t('laser.page.history');
  const machines = await get('/machines');
  const mid = +(args[0] || localStorage.hMid || machines[0].id);
  const bucket = localStorage.hBucket || 'day';
  const start = localStorage.hStart || daysAgo(30), end = localStorage.hEnd || today();
  $('#view').innerHTML = `
    <div class="toolbar">
      Machine <select id="hm" onchange="localStorage.hMid=this.value;route()">
        ${machines.map(m => `<option value="${m.id}" ${m.id === mid ? 'selected' : ''}>${esc(m.name)}</option>`).join('')}</select>
      Bucket <select onchange="localStorage.hBucket=this.value;route()">
        ${['hour', 'shift', 'day', 'week', 'month'].map(b => `<option ${b === bucket ? 'selected' : ''}>${b}</option>`).join('')}</select>
      From <input type="date" value="${start}" onchange="localStorage.hStart=this.value;route()">
      To <input type="date" value="${end}" onchange="localStorage.hEnd=this.value;route()">
      <span class="right">
        ${[7, 30, 90, 365].map(d => `<button onclick="localStorage.hStart='${daysAgo(d)}';localStorage.hEnd='${today()}';route()">${d}d</button>`).join(' ')}
      </span></div>
    <div id="hbody"><div class="empty">Loading…</div></div>`;
  const h = await get(`/machines/${mid}/history?start=${start}&end=${end}&bucket=${bucket}`);
  const r = h.rows, L = r.map(x => String(x.bucket).slice(bucket === 'hour' ? 5 : 0));
  const tot = r.reduce((a, x) => ({units: a.units + (x.units || 0), jobs: a.jobs + (x.jobs || 0),
    busy: a.busy + (x.busy_seconds || 0), idle: a.idle + (x.idle_seconds || 0),
    down: a.down + (x.down_seconds || 0), alarms: a.alarms + (x.alarm_count || 0),
    laser: a.laser + (x.laser_seconds || 0)}), {units: 0, jobs: 0, busy: 0, idle: 0, down: 0, alarms: 0, laser: 0});
  $('#hbody').innerHTML = `
    <div class="kpis">
      ${kpi(t('laser.kpi.units'), n0(tot.units), `${n0(tot.jobs)} jobs`, 'accent')}
      ${kpi(t('laser.kpi.running_time'), n1(tot.busy / 3600) + ' h', 'job execution')}
      ${kpi(t('laser.kpi.laser_on_time'), n1(tot.laser / 3600) + ' h', pct(tot.busy ? tot.laser / tot.busy * 100 : null) + ' duty')}
      ${kpi(t('laser.kpi.idle_time'), n1(tot.idle / 3600) + ' h', '', 'warn')}
      ${kpi(t('laser.kpi.down_time'), n1(tot.down / 3600) + ' h', '', 'bad')}
      ${kpi(t('laser.kpi.alarms'), n0(tot.alarms), '')}
      ${kpi(t('laser.kpi.avg_per_period'), n1(tot.units / Math.max(1, r.length)), bucket)}
    </div>
    <div class="panel"><h3>${esc(t('laser.panel.output'))}</h3><div class="body">${svgBar(L, [{name: 'Units', data: r.map(x => x.units)}], {height: 210, rotate: L.length > 20})}</div></div>
    <div class="panel"><h3>${esc(t('laser.panel.time_breakdown'))}</h3><div class="body">${svgBar(L, [
      {name: 'Running', data: r.map(x => (x.busy_seconds || 0) / 3600), color: 'var(--ok)'},
      {name: 'Idle', data: r.map(x => (x.idle_seconds || 0) / 3600), color: 'var(--warn)'},
      {name: 'Down', data: r.map(x => (x.down_seconds || 0) / 3600), color: 'var(--bad)'}],
      {height: 220, stacked: true, rotate: L.length > 20})}</div></div>
    ${r[0] && 'utilization' in r[0] ? `<div class="panel"><h3>${esc(t('laser.panel.kpi_trend'))}</h3><div class="body">${svgLine([
      {name: 'Utilization %', data: r.map(x => x.utilization)},
      {name: 'Performance %', data: r.map(x => x.performance)},
      {name: 'OEE %', data: r.map(x => x.oee)},
      {name: 'Health', data: r.map(x => x.health_score), color: 'var(--maint)'}], {labels: L, height: 210})}</div></div>` : ''}
    ${r[0] && 'temp_galvo_max' in r[0] ? `<div class="panel"><h3>${esc(t('laser.panel.thermal'))} <span class="sub">${esc(t('laser.sub.peak_galvo_servo_temperature_degc'))}</span></h3><div class="body">${svgLine([
      {name: 'Galvo max', data: r.map(x => x.temp_galvo_max || null)},
      {name: 'Servo max', data: r.map(x => x.temp_servo_max || null), color: 'var(--bad)'}], {labels: L, height: 190, min0: false})}</div></div>` : ''}
    <div class="panel"><h3>${esc(t('laser.panel.data'))}</h3><div class="body">${table(
      Object.keys(r[0] || {bucket: 1}).map(k => ({label: k.replace(/_/g, ' '), key: k, num: k !== 'bucket',
        render: k === 'bucket' ? null : (x => typeof x[k] === 'number' ? n1(x[k]) : esc(x[k]))})), r)}</div></div>`;
}

/* ---------------------------------------------------------- 8. alerts */
async function pageAlerts() {
  $('#title').textContent = t('laser.page.alerts');
  const tab = localStorage.alTab || 'active';
  const [list, sum] = await Promise.all([get('/alerts?days=30'), get('/alerts/summary?days=30')]);
  const rules = tab === 'rules' ? await get('/alerts/rules') : [];
  const active = list.filter(a => a.status !== 'resolved');
  paint(`
    <div class="kpis">
      ${kpi(t('laser.kpi.open'), n0(list.filter(a => a.status === 'open').length), esc(t('laser.sub.unacknowledged')), 'bad')}
      ${kpi(t('laser.kpi.acknowledged'), n0(list.filter(a => a.status === 'acknowledged').length), esc(t('laser.sub.being_handled')), 'warn')}
      ${kpi(t('laser.kpi.critical_open'), n0(active.filter(a => a.severity === 'critical').length), '', 'bad')}
      ${kpi(t('laser.kpi.resolved_30d'), n0(list.filter(a => a.status === 'resolved').length), '')}
      ${kpi(t('laser.kpi.avg_resolution'), n1(avg(list.filter(a => a.duration_seconds).map(a => a.duration_seconds / 60))) + ' min', '')}
    </div>
    <div class="tabs">${['active', 'history', 'analysis', 'rules']
      .map(k => `<a href="#" onclick="localStorage.alTab='${k}';route();return false" class="${k === tab ? 'on' : ''}">${esc(t('laser.tab.' + k))}</a>`).join('')}</div>
    ${tab === 'active' ? groupedAlerts(active)
      : tab === 'history' ? alertTable(list, false)
      : tab === 'analysis' ? `<div class="grid2">
          <div class="panel"><h3>${esc(t('laser.panel.by_type'))}</h3><div class="body">${svgBar(sum.by_type.map(r => short(tAlertType(r.type), 18)), [{name: 'Count', data: sum.by_type.map(r => r.n)}], {height: 220, rotate: true})}
            ${table([{ label: t('laser.col.type'), key: 'type', render: r => esc(tAlertType(r.type))}, {label: t('laser.col.severity'), key: 'severity', render: r => sevBadge(r.severity)},
                     {label: t('laser.col.count'), key: 'n', num: true}, { label: t('laser.col.avg_min_open'), key: 'avg_minutes', num: true, render: r => n1(r.avg_minutes)}], sum.by_type)}</div></div>
          <div class="panel"><h3>${esc(t('laser.panel.by_machine'))}</h3><div class="body">${svgBar(sum.by_machine.map(r => short(r.name || '-')), [{name: 'Alerts', data: sum.by_machine.map(r => r.n)}], {height: 220, rotate: true})}
            ${table([{label: t('laser.col.machine'), key: 'name'}, { label: t('laser.nav.alerts'), key: 'n', num: true}, {label: t('laser.col.critical'), key: 'critical', num: true}], sum.by_machine)}</div></div>
          <div class="panel" style="grid-column:1/-1"><h3>${esc(t('laser.panel.daily_alert_volume'))}</h3><div class="body">${svgLine([
            {name: 'All', data: sum.daily.map(r => r.n), area: true}, {name: 'Critical', data: sum.daily.map(r => r.critical), color: 'var(--bad)'}],
            {labels: sum.daily.map(r => r.d.slice(5)), height: 200})}</div></div></div>`
      : rulesTable(rules)}`);
}

/* One switched-off PC used to open eight alerts, so the flat list was 170 rows of
   the same handful of facts. Top band = one line per condition class; below it one
   collapsed group per machine. Groups are separate <details>, never a shared tbody:
   sortTable() re-sorts a whole tbody and would scramble headers into the data. */
function groupedAlerts(rows) {
  if (!rows.length) return `<div class="panel"><h3>${esc(t('laser.panel.active_alerts'))}</h3><div class="body"><div class="empty">${esc(t('laser.msg.no_alerts'))}</div></div></div>`;
  const by = k => rows.reduce((m, a) => ((m[a[k]] = m[a[k]] || []).push(a), m), {});
  const rank = g => Math.min(...g.map(a => sevRank(a.severity)));
  const order = (a, b) => rank(a[1]) - rank(b[1]) || b[1].length - a[1].length;
  const worst = g => g.reduce((s, a) => sevRank(a.severity) < sevRank(s) ? a.severity : s, 'ok');
  const open = new Set((localStorage.alGroups || '').split(',').filter(Boolean));

  const band = Object.entries(by('type')).sort(order).map(([ty, g]) => {
    const names = [...new Set(g.map(a => a.machine_name || '?'))];
    return `<div class="asum" title="${esc(names.join(', '))}">${sevBadge(worst(g))}<b>${esc(tAlertType(ty))}</b>
      <span class="hint">${esc(t('laser.msg.n_machines', {n: names.length}))}</span>
      <span class="right hint">${esc(t('laser.msg.n_alerts', {n: g.length}))}</span></div>`;
  }).join('');

  const groups = Object.entries(by('machine_id')).sort(order).map(([mid, g]) =>
    `<details class="agrp" ${open.has(mid) ? 'open' : ''} ontoggle="alGroupToggle('${esc(mid)}',this.open)">
      <summary>${sevChips(g)}<b>${esc(g[0].machine_name || '?')}</b>
        <span class="right hint">${esc(t('laser.msg.n_alerts', {n: g.length}))}</span></summary>
      <div class="agrpb">${alertRows(g, true, true)}</div></details>`).join('');

  return `<div class="panel"><h3>${esc(t('laser.panel.by_type'))}</h3><div class="body">${band}</div></div>
    <div class="panel"><h3>${esc(t('laser.panel.active_alerts'))}</h3><div class="body">${groups}</div></div>`;
}

/* counts per severity, worst first — glyph + number, never colour alone */
function sevChips(g) {
  return Object.keys(SEV_RANK).map(s => {
    const n = g.filter(a => a.severity === s).length;
    return n ? `<span class="badge b-${s}" title="${esc(tSev(s))}">${sic(SEV_ICON[s] || 'ℹ')}${n}</span>` : '';
  }).join('');
}

function alGroupToggle(mid, on) {
  const s = new Set((localStorage.alGroups || '').split(',').filter(Boolean));
  if (on) s.add(mid); else s.delete(mid);
  localStorage.alGroups = [...s].join(',');
}

function alertTable(rows, actions) {
  return `<div class="panel"><h3>${esc(t(actions ? 'laser.panel.active_alerts' : 'laser.panel.alert_history'))}</h3><div class="body">${alertRows(rows, actions)}</div></div>`;
}

/* grouped = inside an expanded machine group: the machine column is the header, and
   the message is shown whole so the server's " Also masking: …" sentence is readable. */
function alertRows(rows, actions, grouped) {
  return table([
    {label: t('laser.col.severity'), key: 'severity', render: a => sevBadge(a.severity)},
    ...(grouped ? [] : [{label: t('laser.col.machine'), key: 'machine_name'}]),
    {label: t('laser.col.title'), key: 'title'},
    {label: t('laser.col.message'), key: 'message', render: a => alertMsg(a.message, grouped)},
    {label: t('laser.col.started'), key: 'started_at'},
    {label: t('laser.col.duration'), key: 'duration_seconds', render: a => a.duration_seconds ? dur(a.duration_seconds) : (a.status === 'resolved' ? '&mdash;' : dur((Date.now() - new Date(a.started_at.replace(' ', 'T')).getTime()) / 1000))},
    {label: t('laser.col.status'), key: 'status', render: a => `<span class="badge b-${esc(a.status)}">${esc(a.status)}</span>`},
    {label: t('laser.col.owner'), key: 'ack_by', render: a => esc(a.ack_by || a.assigned_to || '')},
    {label: t('laser.col.resolution'), key: 'resolution'},
    ...(actions && can('ack') ? [{label: '', key: 'x', render: a =>
      `${a.status === 'open' ? `<button onclick="event.stopPropagation();ackAlert(${a.id})">${esc(t('laser.btn.ack'))}</button>` : ''}
       <button onclick="event.stopPropagation();resolveAlert(${a.id})">${esc(t('laser.btn.resolve'))}</button>`}] : []),
  ], rows, {empty: t('laser.msg.no_alerts')});
}

/* The server appends " Also masking: …" to a root alert so nothing is hidden silently.
   The flat table's 90-char cut would swallow it; an expanded group has the room. */
const MASK_NOTE = ' Also masking: ';
function alertMsg(m, grouped) {
  m = m || '';
  if (!grouped) return `<span title="${esc(m)}">${esc(m.slice(0, 90))}</span>`;
  const i = m.indexOf(MASK_NOTE);
  return i < 0 ? esc(m) : esc(m.slice(0, i)) +
    `<div class="masked"><b>${esc(t('laser.msg.also_masking'))}:</b> ${esc(m.slice(i + MASK_NOTE.length))}</div>`;
}

async function ackAlert(id) { await post(`/alerts/${id}/ack`); toast(t('laser.msg.acknowledged'), 'ok'); route(); }
async function resolveAlert(id) {
  const r = prompt(t('laser.msg.resolution_note'), t('laser.msg.handled'));
  if (r === null) return;
  await post(`/alerts/${id}/resolve`, {resolution: r}); toast(t('laser.msg.resolved'), 'ok'); route();
}

function rulesTable(rules) {
  const editable = can('alerts');
  return `<div class="panel"><h3>${esc(t('laser.panel.alert_rules'))} <span class="sub">${esc(t('laser.panel.rules_sub'))}</span></h3><div class="body">
    ${table([
      { label: t('laser.col.type'), key: 'type', render: r => esc(tAlertType(r.type))},
      { label: t('laser.col.scope'), key: 'machine_name', render: r => esc(r.machine_name || 'All machines')},
      { label: t('laser.col.on'), key: 'enabled', render: r => editable
        ? `<input type="checkbox" ${r.enabled ? 'checked' : ''} style="width:auto" onchange="saveRule(${r.id},{enabled:this.checked?1:0})">`
        : (r.enabled ? 'yes' : 'no')},
      {label: t('laser.col.severity'), key: 'severity', render: r => editable
        ? `<select onchange="saveRule(${r.id},{severity:this.value})">${['info', 'warning', 'critical'].map(s => `<option ${s === r.severity ? 'selected' : ''}>${s}</option>`).join('')}</select>`
        : sevBadge(r.severity)},
      { label: t('laser.col.threshold'), key: 'threshold', num: true, render: r => editable
        ? `<input value="${r.threshold === null ? '' : r.threshold}" style="width:80px" onchange="saveRule(${r.id},{threshold:parseFloat(this.value)})">` : n1(r.threshold)},
      { label: t('laser.col.window_min'), key: 'window_min', num: true, render: r => editable
        ? `<input value="${r.window_min === null ? '' : r.window_min}" style="width:66px" onchange="saveRule(${r.id},{window_min:parseInt(this.value)||null})">` : n0(r.window_min)},
      { label: t('laser.col.cooldown_min'), key: 'cooldown_min', num: true, render: r => editable
        ? `<input value="${r.cooldown_min === null ? '' : r.cooldown_min}" style="width:66px" onchange="saveRule(${r.id},{cooldown_min:parseInt(this.value)||null})">` : n0(r.cooldown_min)},
      { label: t('laser.col.channels'), key: 'channels', render: r => editable
        ? `<input value="${esc(r.channels || '')}" style="width:130px" placeholder="app,email,teams" onchange="saveRule(${r.id},{channels:this.value})">` : esc(r.channels)},
      { label: t('laser.col.recipients'), key: 'recipients', render: r => editable
        ? `<input value="${esc(r.recipients || '')}" style="width:190px" placeholder="a@x.com,b@x.com" onchange="saveRule(${r.id},{recipients:this.value})">` : esc(r.recipients)},
      { label: t('laser.col.escalate_after'), key: 'escalate_after_min', num: true, render: r => editable
        ? `<input value="${r.escalate_after_min === null ? '' : r.escalate_after_min}" style="width:66px" onchange="saveRule(${r.id},{escalate_after_min:parseInt(this.value)||null})">` : n0(r.escalate_after_min)},
      { label: t('laser.col.escalate_to'), key: 'escalate_to', render: r => editable
        ? `<input value="${esc(r.escalate_to || '')}" style="width:170px" onchange="saveRule(${r.id},{escalate_to:this.value})">` : esc(r.escalate_to)},
      { label: t('laser.col.what_it_detects'), key: 'description'},
    ], rules)}
    ${editable ? `<div class="toolbar" style="margin-top:10px"><button onclick="post('/alerts/evaluate').then(r=>{toast(r.raised+' alert(s) raised','ok');route()})">Run rules now</button>
      <span class="hint">Rules are evaluated automatically every 60 seconds.</span></div>` : ''}
  </div></div>`;
}
async function saveRule(id, patch) { try { await put('/alerts/rules/' + id, patch); toast('Rule saved', 'ok'); } catch (e) { toast(e.message, 'bad'); } }

/* --------------------------------------------------------- 9. reports */
async function pageReports() {
  $('#title').textContent = t('laser.page.reports');
  const list = await get('/reports');
  $('#view').innerHTML = `
    <div class="panel"><h3>${esc(t('laser.panel.report_library'))} <span class="sub">${esc(t('laser.panel.report_library_sub'))}</span></h3><div class="body">
      <div class="toolbar">Period <select id="rdays">${[1, 7, 14, 30, 90, 365].map(d => `<option value="${d}" ${d === 30 ? 'selected' : ''}>last ${d} days</option>`).join('')}</select>
        Date <input type="date" id="rdate" value="${today()}" style="width:auto"></div>
      <div class="mgrid">${list.map(r => `<div class="mc" style="border-left-color:var(--accent);cursor:default">
        <div class="h"><b>${esc(r.title)}</b></div>
        <div style="display:flex;gap:6px;margin-top:8px">
          <button onclick="previewReport('${r.key}')">Preview</button>
          <button onclick="downloadReport('${r.key}','xlsx')">Excel</button>
          <button onclick="downloadReport('${r.key}','pdf')">PDF</button>
        </div></div>`).join('')}</div></div></div>
    <div id="rprev"></div>`;
}
function reportQS(days) {
  const d = days || ($('#rdays') ? $('#rdays').value : 30);
  const dt = $('#rdate') ? $('#rdate').value : today();
  return `days=${d}&date=${dt}`;
}
async function previewReport(key) {
  busy(true);
  try {
    const rep = await get(`/reports/${key}?fmt=json&${reportQS()}`);
    $('#rprev').innerHTML = `<div class="panel"><h3>${esc(rep.title)} <span class="sub">${esc(rep.subtitle || '')}</span>
      <span class="right"><button onclick="downloadReport('${key}','xlsx')">Excel</button>
      <button onclick="downloadReport('${key}','pdf')">PDF</button></span></h3><div class="body">
      ${rep.sections.map(s => `<h4 style="margin:12px 0 6px;font-size:12px;color:var(--muted)">${esc(s.name)}</h4>
        ${table(s.columns.map((c, i) => ({label: c, key: i, num: typeof (s.rows[0] || [])[i] === 'number'})),
          s.rows.map(r => Object.fromEntries(r.map((v, i) => [i, v]))))}`).join('')}</div></div>`;
    $('#rprev').scrollIntoView({behavior: 'smooth'});
  } catch (e) { toast(e.message, 'bad'); }
  busy(false);
}
function downloadReport(key, fmt, days) { window.open(`/api/reports/${key}?fmt=${fmt}&${reportQS(days)}`, '_blank'); }

/* ------------------------------------------- 10. machine configuration */
async function pageMachines() {
  $('#title').textContent = t('laser.page.machines');
  const ms = await get('/machines');
  $('#view').innerHTML = `
    <div class="toolbar">
      <button class="primary" onclick="editMachine(null)">+ Add machine</button>
      <button onclick="testAll()">Test all connections</button>
      <button onclick="suggestTargets()">Set daily targets…</button>
      <span class="right hint">Credentials are stored encrypted. Changing a host or password takes effect on the next cycle — no code change and no restart.</span></div>
    <div class="panel"><h3>${esc(t('laser.panel.machines'))} <span class="sub">${ms.length} configured, ${ms.filter(m => m.enabled).length} enabled</span></h3><div class="body">
      ${table([
        { label: t('laser.col.id'), key: 'id', num: true}, {label: t('laser.col.code'), key: 'code'}, {label: t('laser.col.name'), key: 'name'},
        {label: t('laser.col.host'), key: 'host'}, {label: t('laser.col.port'), key: 'port', num: true},
        {label: t('laser.col.share'), key: 'share'}, {label: t('laser.col.db_file'), key: 'db_file'},
        {label: t('laser.col.user'), key: 'username'}, {label: t('laser.col.password'), key: 'password_set', render: m => m.password_set ? '••••••' : '<span class="hint">not set</span>'},
        {label: t('laser.col.method'), key: 'connection_method'},
        {label: t('laser.col.model'), key: 'model', render: m => esc(m.model || m.detected_model || '')},
        {label: t('laser.col.serial'), key: 'serial_number', render: m => esc(m.serial_number || m.detected_serial || '')},
        {label: t('laser.col.location'), key: 'location'}, {label: t('laser.col.area'), key: 'production_area'},
        {label: t('laser.col.status'), key: 'status'},
        { label: t('laser.col.hb_s'), key: 'heartbeat_interval', num: true}, { label: t('laser.col.sync_s'), key: 'sync_interval', num: true},
        { label: t('laser.col.timeout'), key: 'timeout_seconds', num: true},
        { label: t('laser.col.retry'), key: 'retry_max', num: true, render: m => `${m.retry_max}×${m.retry_backoff}s`},
        { label: t('laser.col.target_day'), key: 'target_units_day', num: true},
        {label: t('laser.col.enabled'), key: 'enabled', render: m => m.enabled ? '<span class="badge b-ok">yes</span>' : '<span class="badge b-critical">no</span>'},
        {label: t('laser.col.link'), key: 'connectivity', render: m => `<span class="badge b-${m.connectivity === 'CONNECTED' ? 'ok' : m.connectivity === 'SLOW' ? 'warning' : 'critical'}">${esc(m.connectivity || '-')}</span>`},
        {label: t('laser.col.last_ok'), key: 'last_heartbeat_ok', render: m => esc(ago(m.last_heartbeat_ok))},
        {label: '', key: 'x', render: m => `<button onclick="event.stopPropagation();editMachine(${m.id})">Edit</button>
          <button onclick="event.stopPropagation();testMachine(${m.id})">Test</button>
          <button onclick="event.stopPropagation();syncMachine(${m.id})">Sync</button>
          <button onclick="event.stopPropagation();showLogs(${m.id})">Logs</button>`},
      ], ms)}</div></div>
    <div id="testout"></div>`;
}

async function editMachine(id) {
  const ms = await get('/machines');
  const m = id ? ms.find(x => x.id === id) : {port: 445, share: 'JeanologiaDB', db_file: 'stats.db',
    username: 'Admin', connection_method: 'smb', status: 'active', production_area: 'Laser',
    heartbeat_interval: 60, sync_interval: 120, timeout_seconds: 10, retry_max: 3, retry_backoff: 30,
    enabled: 1, machine_type: 'laser'};
  const f = (k, label, type, opts) => type === 'select'
    ? `<div><label>${label}</label><select name="${k}">${opts.map(o => `<option ${String(m[k]) === String(o) ? 'selected' : ''}>${o}</option>`).join('')}</select></div>`
    : `<div><label>${label}</label><input name="${k}" type="${type || 'text'}" value="${m[k] === null || m[k] === undefined ? '' : esc(m[k])}"></div>`;
  showModal(id ? `Edit ${esc(m.name)}` : 'Add machine', `
    <div class="fgrid">
      ${f('name', 'Machine name')} ${f('code', 'Machine ID / asset code')}
      ${f('host', 'IP address or hostname')} ${f('port', 'Port', 'number')}
      ${f('share', 'Share name')} ${f('db_file', 'Database file')}
      ${f('username', 'Username')}
      <div><label>Password ${id ? '(leave blank to keep)' : ''}</label><input name="password" type="password" placeholder="${m.password_set ? '••••••' : ''}"></div>
      ${f('connection_method', 'Connection method', 'select', ['smb', 'http_push', 'disabled'])}
      ${f('machine_type', 'Machine type', 'select', ['laser', 'compact', 'flexi', 'other'])}
      ${f('model', 'Model')} ${f('serial_number', 'Serial number')}
      ${f('location', 'Location')} ${f('production_area', 'Production area')}
      ${f('status', 'Status', 'select', ['active', 'maintenance', 'disabled'])}
      ${f('heartbeat_interval', 'Heartbeat interval (s)', 'number')}
      ${f('sync_interval', 'Sync interval (s)', 'number')}
      ${f('timeout_seconds', 'Timeout (s)', 'number')}
      ${f('retry_max', 'Retry attempts', 'number')}
      ${f('retry_backoff', 'Retry backoff (s)', 'number')}
      ${f('target_units_day', 'Daily target (units)', 'number')}
      ${f('ideal_seconds_unit', 'Ideal seconds/unit (blank = learn)', 'number')}
      ${f('push_token', 'Push token (http_push only)')}
      <div><label>Enabled</label><select name="enabled"><option value="1" ${m.enabled ? 'selected' : ''}>yes</option><option value="0" ${!m.enabled ? 'selected' : ''}>no</option></select></div>
    </div>
    <label>Notes</label><textarea name="notes" rows="2">${esc(m.notes || '')}</textarea>`,
    [{ label: t('laser.btn.test_connection'), fn: () => id && testMachine(id)},
     { label: t('laser.btn.save'), primary: true, fn: async (vals) => {
       const body = {};
       for (const [k, v] of Object.entries(vals)) {
         if (v === '' && k !== 'notes') continue;
         body[k] = ['port', 'heartbeat_interval', 'sync_interval', 'timeout_seconds', 'retry_max',
                    'retry_backoff', 'target_units_day', 'enabled'].includes(k) ? parseInt(v)
                 : k === 'ideal_seconds_unit' ? parseFloat(v) : v;
       }
       if (id) await put('/machines/' + id, body); else await post('/machines', body);
       toast('Saved', 'ok'); closeModal(); route();
     }}]);
}

/* Daily targets drive target %, the "on track" answer and the target_risk alert.
   They ship at 0, which silently disables all three — this proposes a number from
   each machine's own history rather than inventing one. */
async function suggestTargets() {
  busy(true);
  let d;
  try { d = await get('/machines/targets/suggest?days=30&percentile=50'); }
  catch (e) { busy(false); toast(e.message, 'bad'); return; }
  busy(false);
  const rows = d.rows.map(r => `<tr>
      <td>${esc(r.name)}</td>
      <td class="num">${n0(r.current_target)}</td>
      <td class="num">${r.producing_days}</td>
      <td class="num">${n0(r.median)}</td>
      <td class="num">${n0(r.best_day)}</td>
      <td class="num"><input data-t="${r.machine_id}" value="${r.suggested === null ? (r.current_target || '') : r.suggested}"
          style="width:88px;text-align:right" ${r.suggested === null ? 'placeholder="too little data"' : ''}></td>
    </tr>`).join('');
  showModal('Set daily production targets', `
    <div class="hint" style="margin-bottom:10px">Suggested from the median of each machine's own
      producing days over the last 30 days. Edit any value before applying; 0 disables the target
      for that machine.</div>
    <div class="tw"><table><thead><tr>
      <th>Machine</th><th class="num">Current</th><th class="num">Producing days</th>
      <th class="num">Median/day</th><th class="num">Best day</th><th class="num">New target</th>
    </tr></thead><tbody>${rows}</tbody></table></div>`,
    [{ label: t('laser.btn.apply_targets'), primary: true, fn: async () => {
      const targets = {};
      document.querySelectorAll('[data-t]').forEach(i => {
        const v = parseInt(i.value, 10);
        if (!isNaN(v)) targets[i.dataset.t] = v;
      });
      const r = await post('/machines/targets', {targets});
      toast(`Targets applied to ${r.applied} machines`, 'ok');
      closeModal();
      await post('/analytics/rebuild?days=7').catch(() => {});
      route();
    }}]);
}

async function testMachine(id) {
  busy(true);
  try {
    const r = await post(`/machines/${id}/test`);
    const html = `<div class="panel"><h3>Connection test — ${esc(r.machine)} (${esc(r.host || r.method)})
      <span class="right"><span class="badge b-${r.ok ? 'ok' : 'critical'}">${r.ok ? 'PASS' : 'FAIL'}</span></span></h3><div class="body">
      ${table([{label: t('laser.col.step'), key: 'step'}, {label: t('laser.col.result'), key: 'ok', render: s => `<span class="badge b-${s.ok ? 'ok' : 'critical'}">${s.ok ? 'ok' : 'fail'}</span>`},
               {label: t('laser.col.detail'), key: 'detail', render: s => `<span class="mono">${esc(s.detail)}</span>`}], r.steps)}</div></div>`;
    if ($('#testout')) { $('#testout').innerHTML = html; $('#testout').scrollIntoView({behavior: 'smooth'}); }
    else showModal('Connection test', html, []);
    toast(r.ok ? 'Connection OK' : 'Connection failed', r.ok ? 'ok' : 'bad');
  } catch (e) { toast(e.message, 'bad'); }
  busy(false);
}
async function syncMachine(id) {
  busy(true);
  try { const r = await post(`/machines/${id}/sync`); toast(`Synced: ${r.new || 0} new jobs, ${r.alarms || 0} alarms`, 'ok'); }
  catch (e) { toast(e.message, 'bad'); }
  busy(false);
}
async function testAll() {
  const ms = (await get('/machines')).filter(m => m.enabled);
  busy(true);
  const res = [];
  for (const m of ms) { try { res.push(await post(`/machines/${m.id}/test`)); } catch (e) { res.push({machine: m.name, ok: false, steps: [{step: 'error', ok: false, detail: e.message}]}); } }
  busy(false);
  $('#testout').innerHTML = `<div class="panel"><h3>${esc(t('laser.panel.connection_test_results'))}
    <span class="right">${res.filter(r => r.ok).length}/${res.length} passed</span></h3><div class="body">${table([
    {label: t('laser.col.machine'), key: 'machine'}, {label: t('laser.col.host'), key: 'host'},
    {label: t('laser.col.result'), key: 'ok', render: r => `<span class="badge b-${r.ok ? 'ok' : 'critical'}">${r.ok ? 'PASS' : 'FAIL'}</span>`},
    {label: t('laser.col.detail'), key: 'steps', render: r => `<span class="mono">${esc((r.steps.find(s => !s.ok) || r.steps[r.steps.length - 1] || {}).detail || '')}</span>`}], res)}</div></div>`;
}
async function showLogs(id) {
  const l = await get(`/machines/${id}/logs?limit=150`);
  showModal('Connection & sync logs', `
    <h4 style="font-size:12px;color:var(--muted)">${esc(t('laser.panel.connection_log'))}</h4>
    ${table([{label: t('laser.col.time'), key: 'ts'}, {label: t('laser.col.kind'), key: 'kind'},
             { label: t('laser.sev.ok'), key: 'ok', render: r => `<span class="badge b-${r.ok ? 'ok' : 'critical'}">${r.ok ? 'ok' : 'fail'}</span>`},
             {label: t('laser.col.latency'), key: 'latency_ms', num: true, render: r => n0(r.latency_ms)},
             {label: t('laser.col.rows'), key: 'rows_new', num: true},
             {label: t('laser.col.detail'), key: 'detail', render: r => `<span class="mono">${esc((r.detail || '').slice(0, 120))}</span>`}], l.connection)}
    <h4 style="font-size:12px;color:var(--muted);margin-top:12px">${esc(t('laser.panel.sync_log'))}</h4>
    ${table([{label: t('laser.col.time'), key: 'ts'}, { label: t('laser.col.source'), key: 'source'}, {label: t('laser.col.rows'), key: 'rows_new', num: true},
             { label: t('laser.col.ms'), key: 'duration_ms', num: true, render: r => n0(r.duration_ms)},
             { label: t('laser.sev.ok'), key: 'ok'}, { label: t('laser.col.error'), key: 'error'}], l.sync)}`, []);
}

/* ------------------------------------------------------- 11. settings */
async function pageSettings() {
  $('#title').textContent = t('laser.page.settings');
  const tab = localStorage.stTab || 'thresholds';
  const s = await get('/settings');
  const tabs = [['thresholds', 'Thresholds & shifts'], ['notify', 'Notifications'],
                ['users', 'Users & roles'], ['audit', 'Audit trail'], ['dict', 'Data dictionary']];
  $('#view').innerHTML = `<div class="tabs">${tabs.map(([k, v]) =>
    `<a href="#" onclick="localStorage.stTab='${k}';route();return false" class="${k === tab ? 'on' : ''}">${v}</a>`).join('')}</div><div id="stbody"></div>`;
  const b = $('#stbody');
  const adm = can('settings');
  if (tab === 'thresholds') {
    const keys = ['idle_threshold_seconds', 'down_threshold_seconds', 'offline_after_failures', 'slow_latency_ms',
                  'alarm_recent_minutes', 'planned_hours_per_day', 'planned_break_minutes',
                  'quality_proxy_enabled', 'clock_drift_days', 'connection_log_retention_days',
                  'health_weights', 'department_target_units'];
    b.innerHTML = `<div class="panel"><h3>${esc(t('laser.panel.thresholds'))} <span class="sub">${esc(t('laser.sub.these_drive_state_detection_oee_and_health_s'))}</span></h3><div class="body">
      <div class="fgrid">${keys.map(k => `<div><label>${k.replace(/_/g, ' ')}</label>
        <input id="s_${k}" value="${esc(s[k])}" ${adm ? '' : 'disabled'}></div>`).join('')}</div>
      ${adm ? `<div class="toolbar" style="margin-top:12px"><button class="primary" onclick="saveSettings(${JSON.stringify(keys).replace(/"/g, '&quot;')})">Save</button>
        <button onclick="post('/analytics/rebuild?deep=true').then(()=>toast('Full recompute finished','ok'))">Recompute all analytics</button>
        <button onclick="post('/system/import-legacy').then(r=>toast('Imported '+r.imported+' legacy rows','ok'))">Import legacy central.db</button></div>` : ''}
      <div class="hint" style="margin-top:8px">Health weights format: <span class="mono">connectivity:20,alarms:20,downtime:20,utilization:15,performance:15,thermal:10</span></div>
    </div></div>
    <div class="panel"><h3>${esc(t('laser.panel.shifts'))} <span class="sub">${esc(t('laser.sub.a_shift_crossing_midnight_is_credited_to_the'))}</span></h3><div class="body">
      <table><thead><tr><th>Name</th><th>Start</th><th>End</th><th>Break (min)</th></tr></thead><tbody id="shrows">
      ${s.shifts.map((x, i) => `<tr><td><input value="${esc(x.name)}" data-sh="${i}" data-f="name" ${adm ? '' : 'disabled'}></td>
        <td><input value="${esc(x.start_time)}" data-sh="${i}" data-f="start_time" ${adm ? '' : 'disabled'}></td>
        <td><input value="${esc(x.end_time)}" data-sh="${i}" data-f="end_time" ${adm ? '' : 'disabled'}></td>
        <td><input value="${x.break_minutes}" data-sh="${i}" data-f="break_minutes" ${adm ? '' : 'disabled'}></td></tr>`).join('')}
      </tbody></table>
      ${adm ? '<div class="toolbar" style="margin-top:10px"><button class="primary" onclick="saveShifts()">Save shifts</button></div>' : ''}
    </div></div>`;
  } else if (tab === 'notify') {
    const keys = ['notifications_enabled', 'brevo_api_key', 'brevo_sender_email', 'brevo_sender_name',
                  'teams_webhook', 'whatsapp_endpoint', 'whatsapp_token'];
    b.innerHTML = `<div class="panel"><h3>${esc(t('laser.panel.channels'))} <span class="sub">${esc(t('laser.sub.brevo_transactional_email_microsoft_teams_we'))}</span></h3><div class="body">
      <div class="fgrid">${keys.map(k => `<div><label>${k.replace(/_/g, ' ')}</label>
        <input id="s_${k}" value="${esc(s[k])}" ${adm ? '' : 'disabled'} ${k.includes('key') || k.includes('token') ? 'type="password"' : ''}></div>`).join('')}</div>
      ${adm ? `<div class="toolbar" style="margin-top:12px">
        <button class="primary" onclick="saveSettings(${JSON.stringify(keys).replace(/"/g, '&quot;')})">Save</button>
        <input id="testto" placeholder="test recipient" style="width:220px" value="${esc(ME.email || '')}">
        <button onclick="testChannel('email')">Send test email</button>
        <button onclick="testChannel('teams')">Test Teams</button>
        <button onclick="testChannel('whatsapp')">Test WhatsApp</button></div>` : ''}
      <div class="hint" style="margin-top:8px">Per-rule channels and recipients are set on the <a href="#/alerts" onclick="localStorage.alTab='rules'">Alerts → Rules</a> tab. Escalation fires when an alert stays unacknowledged past its escalation window.</div>
    </div></div>`;
  } else if (tab === 'users') {
    if (!can('users')) { b.innerHTML = '<div class="empty">Administrator access required</div>'; return; }
    const us = await get('/users');
    b.innerHTML = `<div class="panel"><h3>${esc(t('laser.panel.users_and_roles'))}
      <span class="right"><button class="primary" onclick="addUser()">+ Add user</button></span></h3><div class="body">
      ${table([{ label: t('laser.auth.username'), key: 'username'}, {label: t('laser.col.name'), key: 'full_name'}, {label: t('laser.col.email'), key: 'email'},
               {label: t('laser.col.role'), key: 'role', render: u => `<select onchange="put('/users/${u.id}',{role:this.value}).then(()=>toast('Role updated','ok'))">
                 ${['admin', 'manager', 'production', 'maintenance', 'it', 'viewer'].map(r => `<option ${r === u.role ? 'selected' : ''}>${r}</option>`).join('')}</select>`},
               {label: t('laser.col.active'), key: 'active', render: u => `<input type="checkbox" style="width:auto" ${u.active ? 'checked' : ''} onchange="put('/users/${u.id}',{active:this.checked?1:0})">`},
               {label: t('laser.col.last_login'), key: 'last_login'},
               {label: '', key: 'x', render: u => `<button onclick="resetPw(${u.id},'${esc(u.username)}')">Reset password</button>`}], us)}
      <div class="hint" style="margin-top:10px"><b>Roles:</b> admin (everything incl. credentials) · manager (dashboards, reports, acknowledge) ·
        production (live state, acknowledge) · maintenance (health, alarms, acknowledge) · it (connectivity, config, settings) · viewer (read only).
        Machine passwords are only ever shown to admins.</div></div></div>`;
  } else if (tab === 'audit') {
    if (!can('config')) { b.innerHTML = '<div class="empty">Access required</div>'; return; }
    const a = await get('/audit?limit=400');
    b.innerHTML = `<div class="panel"><h3>${esc(t('laser.panel.audit'))} <span class="sub">${esc(t('laser.sub.configuration_changes_logins_exports_acknowl'))}</span></h3><div class="body">
      ${table([{label: t('laser.col.time'), key: 'ts'}, {label: t('laser.col.user'), key: 'username'}, {label: t('laser.col.action'), key: 'action'},
               {label: t('laser.col.entity'), key: 'entity'}, { label: t('laser.col.id'), key: 'entity_id'},
               {label: t('laser.col.before'), key: 'before', render: r => `<span class="mono">${esc((r.before || '').slice(0, 80))}</span>`},
               {label: t('laser.col.after'), key: 'after', render: r => `<span class="mono">${esc((r.after || '').slice(0, 80))}</span>`},
               {label: t('laser.col.ip'), key: 'ip'}], a)}</div></div>`;
  } else {
    const d = await get('/data-dictionary');
    b.innerHTML = `<div class="panel"><h3>${esc(t('laser.panel.dictionary'))} <span class="sub">${esc(t('laser.sub.every_field_collected_from_the_machines_what'))}</span></h3><div class="body">
      ${(d.fields || []).length ? table([
        { label: t('laser.col.source'), key: 'source'}, { label: t('laser.col.field'), key: 'field'}, { label: t('laser.col.platform_column'), key: 'column'},
        { label: t('laser.col.type'), key: 'type'}, { label: t('laser.col.meaning'), key: 'meaning'},
        { label: t('laser.col.real_time'), key: 'realtime'}, { label: t('laser.col.kpi'), key: 'kpi'}, {label: t('laser.col.alert'), key: 'alert'},
        { label: t('laser.tab.forecast'), key: 'forecast'}, { label: t('laser.nav.maintenance'), key: 'maintenance'},
        { label: t('laser.col.productivity'), key: 'productivity'}], d.fields)
        : '<div class="empty">DATA_DICTIONARY.json not found in the platform folder</div>'}</div></div>`;
  }
}
async function saveSettings(keys) {
  const body = {}; keys.forEach(k => body[k] = $('#s_' + k).value);
  try { await put('/settings', body); toast('Settings saved', 'ok'); } catch (e) { toast(e.message, 'bad'); }
}
async function saveShifts() {
  const rows = {};
  document.querySelectorAll('#shrows input').forEach(i => {
    (rows[i.dataset.sh] = rows[i.dataset.sh] || {})[i.dataset.f] = i.dataset.f === 'break_minutes' ? +i.value : i.value;
  });
  await put('/shifts', Object.values(rows)); toast('Shifts saved', 'ok'); route();
}
async function testChannel(ch) {
  busy(true);
  try { const r = await post('/alerts/test-channel', {channel: ch, recipient: $('#testto').value});
    toast(r.ok ? 'Sent' : 'Failed: ' + r.detail, r.ok ? 'ok' : 'bad'); } catch (e) { toast(e.message, 'bad'); }
  busy(false);
}
function addUser() {
  showModal('Add user', `<div class="fgrid">
    <div><label>Username</label><input name="username"></div>
    <div><label>Full name</label><input name="full_name"></div>
    <div><label>Email</label><input name="email" type="email"></div>
    <div><label>Role</label><select name="role">${['viewer', 'production', 'maintenance', 'it', 'manager', 'admin'].map(r => `<option>${r}</option>`).join('')}</select></div>
    <div><label>Password (min 8 chars)</label><input name="password" type="password"></div></div>`,
    [{ label: t('laser.btn.create'), primary: true, fn: async v => { await post('/users', v); toast('User created', 'ok'); closeModal(); route(); }}]);
}
function resetPw(id, name) {
  const p = prompt(`New password for ${name} (min 8 characters):`);
  if (p) put('/users/' + id, {password: p}).then(() => toast('Password reset', 'ok'));
}

/* ------------------------------------------------- 12. machine detail */
async function pageMachineDetail(args) {
  const id = +args[0];
  const d = await get('/machines/' + id);
  const m = d.machine, s = d.state, td = d.today, hb = d.health;
  $('#title').textContent = m.name;
  const hist = await get(`/machines/${id}/history?start=${daysAgo(30)}&end=${today()}&bucket=day`);
  const H = hist.rows, L = H.map(x => x.bucket.slice(5));
  $('#view').innerHTML = `
    <div class="toolbar">
      <span class="badge b-${s.status === 'PRODUCING' ? 'ok' : s.status === 'OFFLINE' || s.status === 'STOPPED' ? 'critical' : 'warning'}">${esc(s.status || '-')}</span>
      <span class="badge b-${s.connectivity === 'CONNECTED' ? 'ok' : 'warning'}">${esc(s.connectivity || '-')}</span>
      <span class="hint">${esc(m.host || m.connection_method)} · ${esc(d.identity.model || m.model || '')} · serial ${esc(d.identity.serial_number || m.serial_number || '-')}
        · eMark ${esc(d.identity.emark_version || '-')} · ${d.identity.n_lasers || '?'} laser(s) · duty max ${d.identity.duty_max || '?'}%</span>
      <span class="right">
        <a class="btn" href="#/history/${id}" onclick="localStorage.hMid=${id}">Historical analysis</a>
        ${can('config') ? `<button onclick="testMachine(${id})">Test</button><button onclick="syncMachine(${id}).then(route)">Sync now</button><button onclick="editMachine(${id})">Configure</button>` : ''}
      </span></div>

    <div class="kpis">
      ${kpi(t('laser.kpi.units_today'), n0(td.units), td.target_units ? `target ${n0(td.target_units)} · ${pct(td.target_pct)}` : `${n0(td.jobs)} jobs`, 'accent')}
      ${kpi('Forecast EOD', n0(d.forecast.projected_units), `${n0(d.forecast.low)}–${n0(d.forecast.high)}`)}
      ${kpi('Utilization', pct(td.utilization), 'busy vs planned')}
      ${kpi(t('laser.kpi.performance'), pct(td.performance), 'vs best cycle time')}
      ${kpi(t('laser.kpi.oee'), pct(td.oee), td.quality_proxy ? `quality proxy ${pct(td.quality_proxy)}` : '')}
      ${kpi(t('laser.kpi.running_idle'), dur(td.busy_seconds) + ' / ' + dur(td.idle_seconds), 'today')}
      ${kpi(t('laser.kpi.downtime'), dur(td.down_seconds), `${n0(td.alarm_count)} alarms`, (td.down_seconds || 0) > 3600 ? 'bad' : '')}
      ${kpi(t('laser.kpi.laser_duty'), pct((td.laser_duty || 0) * 100), 'laser on vs job time')}
    </div>

    <div class="grid3">
      <div class="panel"><h3>${esc(t('laser.panel.machine_health'))}</h3><div class="body">
        ${gauge(td.health_score, 'Health score today')}
        ${hb ? Object.entries(hb.components).map(([k, v]) => `<div style="margin-top:7px">
          <div style="display:flex;justify-content:space-between;font-size:11px"><span style="color:var(--muted)">${esc(k)} <span class="hint">(w ${hb.weights[k] || 0})</span></span><b>${n0(v)}</b></div>
          <div class="bar" style="height:5px;background:rgba(0,0,0,.35);border-radius:3px"><i style="display:block;height:100%;width:${v}%;background:${healthColor(v)}"></i></div></div>`).join('') : ''}
        <div class="hint" style="margin-top:10px">Trend ${n1(d.maintenance_risk.health.slope_per_day)} pts/day → ${n0(d.maintenance_risk.health.in_7_days)} in 7 days</div>
      </div></div>
      <div class="panel"><h3>${esc(t('laser.panel.maintenance_risk'))} <span class="sub">${esc(d.maintenance_risk.level)}</span></h3><div class="body">
        ${gauge(d.maintenance_risk.risk, 'Risk score')}
        <ul style="margin:10px 0 0;padding-left:18px;color:var(--muted)">${d.maintenance_risk.reasons.map(r => `<li>${esc(r)}</li>`).join('') || '<li>No risk indicators</li>'}</ul>
        <div class="hint" style="margin-top:8px">Critical alarms (14 d): ${n0(d.maintenance_risk.critical_alarms_14d)} ·
          head drift ${n1(d.maintenance_risk.head_drift_pct)}% · temp trend ${n1(d.maintenance_risk.temp_slope)} °C/day ·
          expected downtime ${n0(d.downtime_forecast.expected_downtime_minutes)} min/day</div>
      </div></div>
      <div class="panel"><h3>${esc(t('laser.panel.connectivity_2'))}</h3><div class="body">
        ${['status', 'connectivity', 'last_heartbeat_ok', 'latency_ms', 'last_sync_ok', 'last_source_id', 'source_mtime',
           'consecutive_fail', 'current_design', 'current_job_start', 'last_activity', 'status_since', 'last_error']
          .map(k => `<div style="display:flex;justify-content:space-between;padding:2.5px 0;border-bottom:1px solid var(--line);gap:10px">
            <span style="color:var(--muted)">${esc(k.replace(/_/g, ' '))}</span>
            <b class="mono" style="text-align:right;word-break:break-all">${esc(String(s[k] === null || s[k] === undefined ? '—' : s[k]).slice(0, 60))}</b></div>`).join('')}
      </div></div>
    </div>

    <div class="grid2">
      <div class="panel"><h3>${esc(t('laser.panel.30_day_output'))}</h3><div class="body">${svgBar(L, [{name: 'Units', data: H.map(x => x.units)}], {height: 200})}</div></div>
      <div class="panel"><h3>${esc(t('laser.panel.30_day_time_breakdown_hours'))}</h3><div class="body">${svgBar(L, [
        {name: 'Running', data: H.map(x => (x.busy_seconds || 0) / 3600), color: 'var(--ok)'},
        {name: 'Idle', data: H.map(x => (x.idle_seconds || 0) / 3600), color: 'var(--warn)'},
        {name: 'Down', data: H.map(x => (x.down_seconds || 0) / 3600), color: 'var(--bad)'}], {height: 200, stacked: true})}</div></div>
      <div class="panel"><h3>${esc(t('laser.panel.kpi_trend'))}</h3><div class="body">${svgLine([
        {name: 'Utilization %', data: H.map(x => x.utilization)}, {name: 'Performance %', data: H.map(x => x.performance)},
        {name: 'OEE %', data: H.map(x => x.oee)}, {name: 'Health', data: H.map(x => x.health_score), color: 'var(--maint)'}],
        {labels: L, height: 200})}</div></div>
      <div class="panel"><h3>${esc(t('laser.panel.thermal_trend'))} <span class="sub">${esc(t('laser.sub.peak_galvo_servo_temperature_per_day'))}</span></h3><div class="body">${svgLine([
        {name: 'Galvo max °C', data: H.map(x => x.temp_galvo_max || null)},
        {name: 'Servo max °C', data: H.map(x => x.temp_servo_max || null), color: 'var(--bad)'}], {labels: L, height: 200, min0: false})}</div></div>
    </div>

    ${d.open_alerts.length ? `<div class="panel"><h3>${esc(t('laser.kpi.open_alerts'))}</h3><div class="body">${table([
      {label: t('laser.col.severity'), key: 'severity', render: a => sevBadge(a.severity)},
      {label: t('laser.col.title'), key: 'title'}, {label: t('laser.col.message'), key: 'message'}, {label: t('laser.col.started'), key: 'started_at'},
      {label: t('laser.col.status'), key: 'status'},
      ...(can('ack') ? [{label: '', key: 'x', render: a => `<button onclick="ackAlert(${a.id})">Ack</button><button onclick="resolveAlert(${a.id})">Resolve</button>`}] : [])],
      d.open_alerts)}</div></div>` : ''}

    <div class="grid2">
      <div class="panel"><h3>${esc(t('laser.panel.recent_jobs'))}</h3><div class="body">${table([
        {label: t('laser.col.start'), key: 'init_date'}, {label: t('laser.col.end'), key: 'end_date'},
        {label: t('laser.col.design'), key: 'design'}, {label: t('laser.col.copies'), key: 'copies', num: true}, {label: t('laser.col.units'), key: 'units', num: true},
        {label: t('laser.col.elapsed'), key: 'elapsed_seconds', num: true, render: r => dur(r.elapsed_seconds)},
        {label: t('laser.col.laser'), key: 'laser_ms', num: true, render: r => dur((r.laser_ms || 0) / 1000)},
        {label: t('laser.col.duty'), key: 'laser_duty', num: true, render: r => pct((r.laser_duty || 0) * 100)},
        {label: t('laser.col.operator'), key: 'operator'}, {label: t('laser.col.shift'), key: 'shift'},
        {label: t('laser.col.alarms'), key: 'alarm1', num: true, render: r => n0((r.alarm1 || 0) + (r.alarm2 || 0))},
        {label: t('laser.col.galvo_temp'), key: 'temp_galvo_x', num: true, render: r => n1(Math.max(r.temp_galvo_x || 0, r.temp_galvo_y || 0))},
        {label: t('laser.col.servo_temp'), key: 'temp_servo_x', num: true, render: r => n1(Math.max(r.temp_servo_x || 0, r.temp_servo_y || 0))}],
        d.recent_jobs)}</div></div>
      <div class="panel"><h3>${esc(t('laser.panel.recent_alarms'))} <span class="sub">${esc(t('laser.panel.recent_alarms_sub'))}</span></h3><div class="body">${table([
        {label: t('laser.col.time'), key: 'ts'}, {label: t('laser.col.category'), key: 'category'},
        {label: t('laser.col.severity'), key: 'severity', render: a => sevBadge(a.severity)},
        {label: t('laser.col.code'), key: 'error_code', num: true}, {label: t('laser.col.description'), key: 'description'}], d.recent_alarms)}</div></div>
    </div>

    <div class="panel"><h3>${esc(t('laser.panel.state_timeline'))}</h3><div class="body">${table([
      {label: t('laser.col.status'), key: 'status', render: r => `<span class="badge b-${r.status === 'PRODUCING' ? 'ok' : r.status === 'IDLE' ? 'warning' : 'critical'}">${esc(r.status)}</span>`},
      {label: t('laser.col.link'), key: 'connectivity'}, { label: t('laser.lbl.from'), key: 'start_time'}, { label: t('laser.lbl.to'), key: 'end_time'},
      {label: t('laser.col.duration'), key: 'duration_seconds', render: r => dur(r.duration_seconds)}], d.state_history)}</div></div>`;
}

/* ---------------------------------------------------------------- modal */
function showModal(title, body, actions) {
  closeModal();
  const m = el(`<div class="modal" id="modal"><div class="box"><h3>${title}<span class="right"></span></h3>
    <div class="body">${body}</div><div class="foot"></div></div></div>`);
  const foot = m.querySelector('.foot');
  (actions || []).forEach(a => {
    const b = el(`<button class="${a.primary ? 'primary' : ''}">${esc(a.label)}</button>`);
    b.onclick = async () => {
      const vals = {};
      m.querySelectorAll('[name]').forEach(i => vals[i.name] = i.value);
      try { await a.fn(vals); } catch (e) { toast(e.message, 'bad'); }
    };
    foot.appendChild(b);
  });
  const c = el('<button>Close</button>'); c.onclick = closeModal; foot.appendChild(c);
  m.onclick = e => { if (e.target === m) closeModal(); };
  document.body.appendChild(m);
}
function closeModal() { const m = $('#modal'); if (m) m.remove(); }

/* -------------------------------------------------------------- session */
function showLogin() {
  $('#login').hidden = false; $('#app').hidden = true;
  if (TIMER) clearInterval(TIMER);
  stopSiren();                       // a logged-out tab must never keep sounding
  const bar = $('#critbar'); if (bar) bar.classList.remove('show');
}
async function doLogin(e) {
  e.preventDefault();
  try {
    const r = await fetch('/api/auth/login', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({username: $('#lu').value, password: $('#lp').value})});
    if (!r.ok) { $('#lerr').textContent = 'Invalid username or password'; return false; }
    ME = (await r.json()).user; start();
  } catch (err) { $('#lerr').textContent = err.message; }
  return false;
}
async function doLogout() { await post('/auth/logout'); location.reload(); }
function showPassword() {
  showModal('Change password', `<div class="fgrid">
    <div><label>Current password</label><input name="current" type="password"></div>
    <div><label>New password (min 8)</label><input name="new" type="password"></div></div>`,
    [{ label: t('laser.col.change'), primary: true, fn: async v => { await post('/auth/password', v); toast('Password changed', 'ok'); closeModal(); }}]);
}

function start() {
  $('#login').hidden = true; $('#app').hidden = false;
  const b = $('#soundbtn');
  b.textContent = SOUND_ON ? '🔔' : '🔕';
  b.classList.toggle('armed', SOUND_ON); b.classList.toggle('muted', !SOUND_ON);
  b.title = 'Critical alarm sound: ' + (SOUND_ON ? 'on' : 'off');
  $('#langsel').innerHTML = Object.entries(LANGS).map(([c, n]) =>
    `<option value="${c}" ${c === LANG ? 'selected' : ''}>${n}</option>`).join('');
  $('#refreshsel').innerHTML = REFRESH_CHOICES.map(([ms, lbl]) =>
    `<option value="${ms}" ${ms === REFRESH_MS ? 'selected' : ''}>↻ ${lbl}</option>`).join('');
  renderNav(); route(); tick();
  // header poll only rewrites the small status pills + alert badge, so it never
  // disturbs the page; criticals still need catching promptly.
  setInterval(() => { if (!document.hidden) tick(); }, 20000);
}

window.addEventListener('hashchange', route);
(async () => {
  await initI18n();
  try { ME = await get('/auth/me'); start(); } catch (e) { showLogin(); }
})();
