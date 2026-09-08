/* Wall / TV display — a screen for the production floor.
   No chrome, large type, slides rotate on a timer. Data is refetched once per
   full cycle (not per slide) so it can run for days without hammering the API.
   Shares app.js globals: get/esc/n0/n1/pct/ago/short/healthColor/toast/go. */
'use strict';

const WALL = { idx: 0, tick: null, paused: false, data: null, elapsed: 0 };
const WALL_INTERVALS = [3, 5, 8, 12, 20, 30];

function wallSecs() { return +(localStorage.wallSecs || 5); }

/* titles resolve at render time so a language switch retitles the slides */
const WALL_SLIDES = [
  { key: 'fleet', tkey: 'laser.wall.fleet', render: wsFleet },
  { key: 'today', tkey: 'laser.wall.today', render: wsToday },
  { key: 'ranking', tkey: 'laser.wall.ranking', render: wsRanking },
  { key: 'alerts', tkey: 'laser.wall.alerts', render: wsAlerts },
  { key: 'health', tkey: 'laser.wall.health', render: wsHealth },
  { key: 'shift', tkey: 'laser.wall.shift', render: wsShift },
];

async function pageWall() {
  document.querySelector('#title').textContent = 'Wall Display';
  document.body.classList.add('wall');
  document.querySelector('#view').innerHTML = `
    <div id="wall">
      <div id="wallhead">
        <div class="wlogo">🎯</div>
        <div><h2>${esc(t('laser.app.dept'))}</h2><div class="wsub" id="wallsub">&mdash;</div></div>
        <div style="flex:1"></div>
        <div id="walldots"></div>
        <div class="wctl">
          <select onchange="localStorage.wallSecs=this.value;wallRestart()" style="width:auto">
            ${WALL_INTERVALS.map(s => `<option value="${s}" ${s === wallSecs() ? 'selected' : ''}>${s}s</option>`).join('')}
          </select>
          <button class="iconbtn" id="wallpause" onclick="wallPause()" title="Pause">⏸</button>
          <button class="iconbtn" onclick="wallGo(-1)" title="Previous">‹</button>
          <button class="iconbtn" onclick="wallGo(1)" title="Next">›</button>
          <button class="iconbtn" onclick="wallFull()" title="Full screen">⛶</button>
          <button class="iconbtn" onclick="exitWall()" title="Exit">✕</button>
        </div>
        <div><div class="wclock" id="wallclock">--:--</div><div class="wdate" id="walldate"></div></div>
      </div>
      <div id="wallbody"><div class="empty">Loading…</div></div>
      <div id="wallprog"><i></i></div>
    </div>`;
  await wallLoad();
  wallPaint();
  wallRestart();
}

async function wallLoad() {
  try {
    const [ov, att, rank, alerts, shifts, hourly] = await Promise.all([
      get('/overview'), get('/attention'), get('/analytics/ranking?days=7'),
      get('/alerts?status=open&days=3'), get('/analytics/shifts?days=7'),
      get('/analytics/hourly')]);
    WALL.data = { ov, att, rank: rank.rows, alerts, shifts, hourly };
    CACHE.ov = ov;
  } catch (e) { /* keep showing the last good data rather than blanking the screen */ }
}

function wallRestart() {
  if (WALL.tick) clearInterval(WALL.tick);
  WALL.elapsed = 0;
  const step = 100;
  WALL.tick = setInterval(() => {
    if (!document.querySelector('#wall')) { clearInterval(WALL.tick); WALL.tick = null; return; }
    const c = document.querySelector('#wallclock'), dt = document.querySelector('#walldate');
    if (c) c.textContent = new Date().toLocaleTimeString();
    if (dt) dt.textContent = new Date().toLocaleDateString(undefined,
      { weekday: 'long', day: 'numeric', month: 'short' });
    if (WALL.paused) return;
    WALL.elapsed += step;
    const bar = document.querySelector('#wallprog i');
    if (bar) bar.style.width = Math.min(100, WALL.elapsed / (wallSecs() * 1000) * 100) + '%';
    if (WALL.elapsed >= wallSecs() * 1000) { WALL.elapsed = 0; wallGo(1); }
  }, step);
}

async function wallGo(dir) {
  if (!document.querySelector('#wall')) return;
  const next = (WALL.idx + dir + WALL_SLIDES.length) % WALL_SLIDES.length;
  if (dir > 0 && next === 0) await wallLoad();      // refresh once per full cycle
  WALL.idx = next;
  WALL.elapsed = 0;
  wallPaint();
}

function wallPause() {
  WALL.paused = !WALL.paused;
  const b = document.querySelector('#wallpause');
  if (b) { b.textContent = WALL.paused ? '▶' : '⏸'; b.title = WALL.paused ? 'Play' : 'Pause'; }
}

function wallFull() {
  if (document.fullscreenElement) document.exitFullscreen();
  else document.documentElement.requestFullscreen().catch(() => toast(t('laser.wall.fullscreen_blocked'), 'bad'));
}

function exitWall() {
  document.body.classList.remove('wall');
  if (WALL.tick) { clearInterval(WALL.tick); WALL.tick = null; }
  go('command');
}

function wallPaint() {
  const d = WALL.data;
  const body = document.querySelector('#wallbody');
  if (!body) return;
  if (!d) { body.innerHTML = '<div class="empty">No data yet</div>'; return; }
  const slide = WALL_SLIDES[WALL.idx];
  const dots = document.querySelector('#walldots');
  if (dots) dots.innerHTML = WALL_SLIDES.map((s, i) => `<b class="${i === WALL.idx ? 'on' : ''}"></b>`).join('');
  const tot = d.ov.totals;
  const sub = document.querySelector('#wallsub');
  if (sub) sub.textContent = `${tot.machines_producing} producing · ${tot.machines_online}/${tot.machines_total} online · ${tot.open_alerts} open alerts`;
  const wall = document.querySelector('#wall');
  if (wall) wall.classList.toggle('hasCrit', (tot.critical_alerts || 0) > 0);
  body.innerHTML = `<div class="wtitle">${esc(t(slide.tkey))}<span class="tag">${esc(d.ov.date)}</span></div>`
    + slide.render(d);
}

function wkpi(label, value, sub, cls) {
  return `<div class="wkpi ${cls || ''}"><div class="l">${esc(label)}</div>
    <div class="v">${value}</div><div class="s">${sub || ''}</div></div>`;
}

/* ------------------------------------------------------------ the slides */
const WALL_ORDER = { PRODUCING: 0, ALARM: 1, IDLE: 2, STOPPED: 3, MAINTENANCE: 4, OFFLINE: 5, UNKNOWN: 6 };

function wsFleet(d) {
  const tot = d.ov.totals;
  const ms = d.ov.machines.slice().sort((a, b) =>
    (WALL_ORDER[a.status] === undefined ? 9 : WALL_ORDER[a.status]) -
    (WALL_ORDER[b.status] === undefined ? 9 : WALL_ORDER[b.status]) || b.units - a.units);
  return `<div class="wkpis">
      ${wkpi(t('laser.wall.producing'), `${tot.machines_producing}/${tot.machines_total}`, t('laser.wall.machines_running'),
             tot.machines_producing >= tot.machines_total * 0.7 ? 'ok' : 'warn')}
      ${wkpi(t('laser.kpi.online'), `${tot.machines_online}`, t('laser.wall.reachable'),
             tot.machines_online >= tot.machines_total - 2 ? 'ok' : 'warn')}
      ${wkpi(t('laser.kpi.units_today'), n0(tot.units), `${n0(tot.jobs)} ${t('laser.wall.jobs')}`)}
      ${wkpi(t('laser.wall.utilization'), pct(tot.utilization), t('laser.wall.busy_vs_planned'), tot.utilization >= 70 ? 'ok' : 'warn')}
      ${wkpi(t('laser.kpi.open_alerts'), n0(tot.open_alerts), `${tot.critical_alerts} ${t('laser.wall.critical')}`,
             tot.critical_alerts ? 'bad' : tot.open_alerts ? 'warn' : 'ok')}
    </div>
    <div class="wtiles">${ms.map(m => `
      <div class="wtile s-${esc(m.status)}">
        <div class="n"><span class="dot"></span>${esc(short(m.name, 18))}</div>
        <div class="u">${n0(m.units)}</div>
        <div class="m">${m.target ? 'of ' + n0(m.target) + ' target' : pct(m.utilization) + ' util'}</div>
        <div class="st">${sic(STATUS_ICON[m.status] || "?")}${esc(m.status)}</div>
      </div>`).join('')}</div>`;
}

function wsToday(d) {
  const tot = d.ov.totals;
  const pctTarget = tot.target ? Math.min(100, tot.units / tot.target * 100) : null;
  const h = d.hourly || [];
  const chart = h.length
    ? svgBar(h.map(r => r.h.slice(0, 2)), [{ name: t('laser.wall.units'), data: h.map(r => r.units) }], { height: 260 })
    : '<div class="empty">No hourly data</div>';
  return `<div class="wgrid2">
    <div class="wpanel">
      <h4>${esc(t('laser.wall.units_produced'))}</h4>
      <div class="wbig" style="color:var(--accent)">${n0(tot.units)}</div>
      <div style="font-size:17px;color:var(--muted);margin-top:10px">
        ${tot.target
          ? `of <b style="color:var(--txt)">${n0(tot.target)}</b> target &nbsp;·&nbsp; <b style="color:${pctTarget >= 95 ? 'var(--ok)' : pctTarget >= 80 ? 'var(--warn)' : 'var(--bad)'}">${pct(pctTarget)}</b>`
          : `${n0(tot.jobs)} jobs completed`}
      </div>
      ${tot.target ? `<div style="height:16px;background:rgba(0,0,0,.35);border-radius:8px;margin-top:16px;overflow:hidden">
        <i style="display:block;height:100%;width:${pctTarget}%;background:var(--grad-accent)"></i></div>` : ''}
      <div style="margin-top:auto;display:grid;grid-template-columns:1fr 1fr;gap:14px">
        <div><div style="color:var(--muted);font-size:13px;letter-spacing:1px">${esc(t('laser.wall.forecast_eod'))}</div>
          <div style="font-size:42px;font-weight:800">${n0(tot.forecast_units)}</div></div>
        <div><div style="color:var(--muted);font-size:13px;letter-spacing:1px">${esc(t('laser.wall.laser_duty'))}</div>
          <div style="font-size:42px;font-weight:800">${pct(tot.laser_duty)}</div></div>
      </div>
    </div>
    <div class="wpanel"><h4>${esc(t('laser.wall.output_by_hour'))}</h4>
      <div style="flex:1">${chart}</div>
      <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-top:10px">
        <div><div style="color:var(--muted);font-size:13px;letter-spacing:1px">${esc(t('laser.wall.downtime'))}</div>
          <div style="font-size:32px;font-weight:800">${n1(tot.down_hours)} h</div></div>
        <div><div style="color:var(--muted);font-size:13px;letter-spacing:1px">${esc(t('laser.wall.idle'))}</div>
          <div style="font-size:32px;font-weight:800">${n1(tot.idle_hours)} h</div></div>
        <div><div style="color:var(--muted);font-size:13px;letter-spacing:1px">${esc(t('laser.wall.alarms'))}</div>
          <div style="font-size:32px;font-weight:800">${n0(tot.alarms)}</div></div>
      </div>
    </div></div>`;
}

function wsRanking(d) {
  const rows = d.rank.slice(0, 10);
  if (!rows.length) return '<div class="empty">No ranking data yet</div>';
  const max = Math.max.apply(null, rows.map(r => r.units || 0).concat([1]));
  return `<div class="wrank">${rows.map((r, i) => `
    <div class="wrow">
      <div class="pos">${i + 1}</div>
      <div class="nm">${esc(short(r.name, 22))}</div>
      <div class="bar"><i style="width:${(r.units || 0) / max * 100}%"></i></div>
      <div class="val">${n0(r.units)}</div>
      <div class="sub">${pct(r.utilization)} util · ${n0(r.health)} health</div>
    </div>`).join('')}</div>
    <div style="color:var(--dim);font-size:14px;margin-top:12px">Units produced over the last 7 days</div>`;
}

function wsAlerts(d) {
  const crit = d.alerts.filter(a => a.severity === 'critical').slice(0, 6);
  const warn = d.alerts.filter(a => a.severity === 'warning').slice(0, 4);
  const list = crit.concat(warn);
  return `<div class="wgrid2">
    <div class="wpanel"><h4>${esc(t('laser.panel.open_alerts'))} (${d.alerts.length})</h4>
      <div style="display:flex;flex-direction:column;gap:9px;overflow:hidden">
      ${list.length ? list.map(a => `
        <div class="walert ${a.severity === 'warning' ? 'warning' : ''}">
          <div class="sev" style="color:${a.severity === 'critical' ? '#f0a3a3' : '#f7d489'}">${sic(SEV_ICON[a.severity] || 'ℹ')}${esc(a.severity.toUpperCase())}</div>
          <div class="mc2">${esc(short(a.machine_name || '-', 20))}</div>
          <div class="tx">${esc(a.title)}</div>
          <div class="ag">${esc(ago(a.started_at))}</div>
        </div>`).join('')
        : '<div style="font-size:26px;color:var(--ok);padding:30px 0">✓ No open alerts</div>'}
      </div></div>
    <div class="wpanel"><h4>${esc(t('laser.wall.needing_attention'))}</h4>
      <div style="display:flex;flex-direction:column;gap:9px;overflow:hidden">
      ${d.att.length ? d.att.slice(0, 8).map(a => `
        <div class="wrow" style="padding:9px 16px">
          <div class="nm" style="width:200px;font-size:17px">${esc(short(a.name, 20))}</div>
          <div class="tx" style="flex:1;font-size:14px;color:var(--muted)">${esc(a.reasons.join(' · '))}</div>
          <div class="val" style="width:70px;font-size:20px;color:${a.score >= 60 ? 'var(--bad)' : 'var(--warn)'}">${a.score}</div>
        </div>`).join('')
        : '<div style="font-size:26px;color:var(--ok);padding:30px 0">✓ All machines normal</div>'}
      </div></div></div>`;
}

function wsHealth(d) {
  const ms = d.ov.machines.filter(m => m.health !== null && m.health !== undefined)
    .slice().sort((a, b) => a.health - b.health).slice(0, 10);
  if (!ms.length) return '<div class="empty">No health data yet</div>';
  return `<div class="wrank">${ms.map(m => `
    <div class="wrow">
      <div class="nm" style="width:250px">${esc(short(m.name, 24))}</div>
      <div class="bar"><i style="width:${m.health}%;background:${healthColor(m.health)}"></i></div>
      <div class="val" style="color:${healthColor(m.health)}">${n0(m.health)}</div>
      <div class="sub">${n0(m.alarms)} alarms · ${n0(m.down_minutes)} min down</div>
    </div>`).join('')}</div>
    <div style="color:var(--dim);font-size:14px;margin-top:12px">Lowest machine health scores today — 100 is perfect</div>`;
}

function wsShift(d) {
  const agg = {};
  d.shifts.forEach(s => {
    const k = s.shift || '-';
    const a = agg[k] || (agg[k] = { units: 0, jobs: 0, down: 0, alarms: 0 });
    a.units += s.units || 0; a.jobs += s.jobs || 0;
    a.down += s.down_hours || 0; a.alarms += s.alarms || 0;
  });
  const names = Object.keys(agg).sort();
  if (!names.length) return '<div class="empty">No shift data yet</div>';
  const max = Math.max.apply(null, names.map(k => agg[k].units).concat([1]));
  return `<div class="wkpis">${names.map(k => wkpi(t('laser.wall.shift') + ' ' + k, n0(agg[k].units),
      `${n0(agg[k].jobs)} jobs · ${n1(agg[k].down)} h down`)).join('')}</div>
    <div class="wrank">${names.map(k => `
      <div class="wrow">
        <div class="nm">${esc(t('laser.wall.shift'))} ${esc(k)}</div>
        <div class="bar"><i style="width:${agg[k].units / max * 100}%"></i></div>
        <div class="val">${n0(agg[k].units)}</div>
        <div class="sub">${n0(agg[k].alarms)} alarms</div>
      </div>`).join('')}</div>
    <div style="color:var(--dim);font-size:14px;margin-top:12px">Totals over the last 7 days</div>`;
}

/* keyboard: space pauses, arrows step, F full screen, Esc exits */
document.addEventListener('keydown', e => {
  if (!document.querySelector('#wall')) return;
  if (e.key === ' ') { e.preventDefault(); wallPause(); }
  else if (e.key === 'ArrowRight') wallGo(1);
  else if (e.key === 'ArrowLeft') wallGo(-1);
  else if (e.key === 'f' || e.key === 'F') wallFull();
  else if (e.key === 'Escape') exitWall();
});
