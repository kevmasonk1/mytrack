/* MyTrack — Supplements (merged from STACK).
   Only does anything for people with Supplements switched on (GET /api/supps/status → on:true).
   For everyone else it adds nothing to the page. It reuses MyTrack's own helpers and styles,
   and hooks in by wrapping a few of index.html's functions (setTab, loadDay, drawHeader, renderToday, renderAdmin). */
(() => {
'use strict';
const START_TAB = new URLSearchParams(location.search).get('tab');
const P = { on: false, status: null, uid: null, view: 'today', data: null, hist: null, watch: null, plan: null, adminQ: '' };

/* ---------- reference: daily upper limits for adults from supplements ----------
   EFSA tolerable upper intake levels (2024 summary), EU SCF, UK FSA advice, EFSA caffeine opinion (2015). */
const LIMITS = {
  'Vitamin A':  { max: 3000, unit: 'mcg', src: 'EFSA upper limit (preformed vitamin A)' },
  'Vitamin D':  { max: 100, unit: 'mcg', src: 'EFSA upper limit (4,000 IU)' },
  'Vitamin E':  { max: 300, unit: 'mg', src: 'EU upper limit' },
  'Vitamin B6': { max: 10, unit: 'mg', src: 'UK advice for supplements (EFSA limit 12 mg)' },
  'Magnesium':  { max: 250, unit: 'mg', src: 'EU upper limit for magnesium from supplements' },
  'Zinc':       { max: 25, unit: 'mg', src: 'EU upper limit' },
  'Selenium':   { max: 255, unit: 'mcg', src: 'EFSA upper limit' },
  'Copper':     { max: 5, unit: 'mg', src: 'EU upper limit' },
  'Boron':      { max: 10, unit: 'mg', src: 'EFSA upper limit' },
  'Iron':       { max: 40, unit: 'mg', src: 'EFSA safe level' },
  'Calcium':    { max: 2500, unit: 'mg', src: 'EFSA upper limit' },
  'Iodine':     { max: 600, unit: 'mcg', src: 'EU upper limit' },
  'Folate':     { max: 1000, unit: 'mcg', src: 'EFSA upper limit for folic acid' },
  'Manganese':  { max: 8, unit: 'mg', src: 'EFSA safe level' },
  'Molybdenum': { max: 0.6, unit: 'mg', src: 'EU upper limit' },
  'Caffeine':   { max: 400, unit: 'mg', src: 'EFSA: up to 400 mg a day for healthy adults' },
  'Creatine':   { max: 10, unit: 'g', src: 'usual maintenance dose is 3–5 g a day' }
};
const UNDER_REVIEW = [[/^ashwagandha/i, 'The UK Food Standards Agency is reviewing its safety (thyroid, liver, blood sugar). No safe level is set in the UK yet.']];
const VIT_RE = /^(Vitamin|Biotin|Folate|Magnesium|Zinc|Selenium|Copper|Iron|Calcium|Potassium|Iodine|Chromium|Boron|Manganese|Molybdenum|Phosphorus|Sodium|Chloride)\b/;
const TO_MG = { g: 1000, mg: 1, mcg: 0.001 };
const conv = (a, f, t) => (TO_MG[f] != null && TO_MG[t] != null) ? a * TO_MG[f] / TO_MG[t] : (f === t ? a : null);
const nf = a => { a = +a || 0; return a >= 100 ? Math.round(a).toLocaleString('en-GB') : a >= 10 ? String(+a.toFixed(1)) : String(+a.toFixed(2)); };
const nowHM = () => { const d = new Date(); return pad(d.getHours()) + ':' + pad(d.getMinutes()); };
const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const WHEN = { all: 'Every day', training: 'Training days', rest: 'Rest days' };
const CHEV = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" style="color:var(--muted);flex:none"><path d="M9 18l6-6-6-6"/></svg>';
const TICK = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.4" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5L20 7"/></svg>';
const icon = p => { const s = ((p?.unit || '') + ' ' + (p?.name || '')).toLowerCase();
  return /shot|coffee|dust|tide|pre-work|whey|protein|milkshake/.test(s) ? '🥤' : /chew|gumm/.test(s) ? '🍬' : /scoop|powder|creatine|glycine|\bg\b/.test(s) ? '🥄' : '💊'; };
const pname = p => p ? ((p.brand && !p.name.toLowerCase().includes(p.brand.toLowerCase()) ? p.brand + ' ' : '') + p.name) : '';

/* ---------- styles (MyTrack theme variables only, so every theme works) ---------- */
const css = document.createElement('style');
css.textContent = `
.nav-in.has-supps{grid-template-columns:1fr 1fr 74px 1fr 1fr 1fr}
.sp-tick{width:30px;height:30px;border-radius:10px;border:2.5px solid var(--line);display:grid;place-items:center;flex:none;color:#fff;transition:background .15s}
.sp-tick.on{background:var(--mint);border-color:var(--mint)}
.sp-row{cursor:pointer}
.sp-row.done .n b{color:var(--muted)}
.sp-info{min-width:34px;height:30px;padding:0 9px;border-radius:99px;background:var(--sky);color:var(--blue);font-weight:900;display:inline-grid;place-items:center;font-size:13px;flex:none}
.sp-chip{display:inline-flex;align-items:center;gap:6px;font-weight:900;font-size:12px;letter-spacing:.4px;text-transform:uppercase;padding:7px 11px;border-radius:99px}
.sp-chip.training{background:var(--mint-bg);color:var(--mint)} .sp-chip.rest{background:var(--sky);color:var(--blue)}
.sp-opt{font-size:11px;font-weight:900;color:var(--muted);background:var(--sky);border-radius:6px;padding:2px 6px;margin-left:6px;vertical-align:2px}
.sp-time{font-size:12px;font-weight:900;width:54px;font-variant-numeric:tabular-nums}
.sp-n{padding:11px 0;border-top:1px solid var(--line)}
.sp-n:first-child{border-top:0}
.sp-n .t2{display:flex;justify-content:space-between;gap:10px;align-items:baseline}
.sp-n .t2 b{font-weight:800}
.sp-n .t2 .a{font-weight:900;white-space:nowrap}
.sp-n .t2 .a span{color:var(--muted);font-weight:700}
.sp-n .s{font-size:12.5px;color:var(--muted);font-weight:600;margin-top:3px}
.sp-bar{position:relative;height:8px;background:var(--sky);border-radius:99px;margin-top:7px}
.sp-bar>i{display:block;height:100%;border-radius:99px;background:linear-gradient(90deg,var(--blue2),var(--blue))}
.sp-bar.near>i{background:var(--sun)} .sp-bar.over>i{background:var(--coral)}
.sp-bar .lim{position:absolute;top:-3px;width:3px;height:14px;border-radius:2px;background:var(--ink)}
.sp-flag{font-size:11px;font-weight:900;letter-spacing:.4px;text-transform:uppercase;padding:3px 8px;border-radius:99px;white-space:nowrap;flex:none}
.sp-flag.over{background:var(--coral-bg);color:var(--coral)} .sp-flag.near,.sp-flag.recent,.sp-flag.dose{background:var(--sun-bg);color:var(--sun)} .sp-flag.review{background:var(--sky);color:var(--blue)}
.sp-watch{border-left:5px solid var(--coral)}
.sp-watch .w{display:flex;gap:10px;align-items:flex-start;padding:10px 0;border-top:1px solid var(--line)}
.sp-watch .w:first-of-type{border-top:0}
.sp-watch .w .n{flex:1;min-width:0} .sp-watch .w b{font-weight:900} .sp-watch .w span{display:block;font-size:13px;color:var(--muted);font-weight:600}
.sp-mac{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:4px}
.sp-mac div{background:var(--sky);border-radius:12px;padding:8px 4px;text-align:center}
.sp-mac b{display:block;font-weight:900;font-size:16px} .sp-mac span{font-size:11px;font-weight:800;color:var(--muted);text-transform:uppercase}
.sp-p{display:flex;align-items:center;gap:12px;width:100%;text-align:left;padding:11px 0;border-top:1px solid var(--line);color:inherit}
.sp-p:first-child{border-top:0}
.sp-p .ph{width:42px;height:42px;border-radius:12px;background:var(--sky);display:grid;place-items:center;font-size:20px;flex:none}
.sp-p .n{flex:1;min-width:0} .sp-p .n b{display:block;font-weight:800;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sp-p .n span{font-size:12.5px;color:var(--muted);font-weight:600}
.sp-ned{display:grid;grid-template-columns:1fr 78px 70px 30px;gap:6px;align-items:center;margin-top:6px}
.sp-ned input,.sp-ned select,.sp-in{width:100%;padding:11px 10px;border-radius:12px;border:2px solid var(--line);background:var(--card);color:var(--ink);font:inherit;font-weight:700;font-size:15px;min-width:0}
.sp-ned .rm{color:var(--coral);font-weight:900;font-size:22px;line-height:1}
.sp-grid2{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.sp-days{display:flex;gap:6px;flex-wrap:wrap}
.sp-days button{flex:1;min-width:40px;padding:9px 0;border-radius:12px;background:var(--bg);color:var(--muted);font-weight:900;font-size:13px}
.sp-days button.on{background:var(--mint);color:#fff}
.sp-weekbars{display:grid;grid-template-columns:repeat(7,1fr);gap:6px;align-items:end;height:130px}
.sp-weekbars>div{display:flex;flex-direction:column;align-items:center;justify-content:flex-end;gap:6px;height:100%}
.sp-weekbars i{display:block;width:100%;border-radius:10px;background:linear-gradient(180deg,var(--blue2),var(--blue));min-height:4px}
.sp-weekbars i.today{background:var(--blue3)} .sp-weekbars i.none{background:var(--sky)}
.sp-weekbars span{font-size:12px;font-weight:800;color:var(--muted)}
.sp-heat{display:grid;grid-template-columns:repeat(7,1fr);gap:5px;margin-top:6px}
.sp-heat i{aspect-ratio:1;border-radius:7px;background:var(--sky);display:block}
.sp-heat i.l1{background:var(--blue3);opacity:.45} .sp-heat i.l2{background:var(--blue3)} .sp-heat i.l3{background:var(--blue)} .sp-heat i.fut{background:transparent;border:2px dashed var(--line)}
.sp-heat b{font-size:11px;font-weight:800;color:var(--muted);text-align:center}
.sp-sw{width:46px;height:28px;border-radius:99px;background:var(--line);position:relative;flex:none;transition:background .15s}
.sp-sw::after{content:'';position:absolute;top:3px;left:3px;width:22px;height:22px;border-radius:50%;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.2);transition:left .15s}
.sp-sw.on{background:var(--mint)} .sp-sw.on::after{left:21px}
.sp-tg{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px 14px;border-radius:14px;border:2px solid var(--line);margin-top:10px;font-weight:800;cursor:pointer}
.sp-tg small{display:block;font-size:12.5px;color:var(--muted);font-weight:600}
.sp-prot{grid-column:1/-1}
.sp-pbar{height:6px;background:var(--sky);border-radius:99px;margin-top:6px;overflow:hidden}
.sp-pbar i{display:block;height:100%;background:var(--mint);border-radius:99px}
.sp-link{color:var(--blue);font-weight:900}
`;
document.head.appendChild(css);

/* ---------- status: work out whether this person has Supplements ---------- */
async function refresh(){
  const uid = S.me?.id;
  if (!uid || uid === P.uid) return;
  P.uid = uid;
  let st = { on: false };
  try { st = await api('/supps/status'); } catch {}
  if (S.me?.id !== uid) return;
  P.on = !!st.on; P.status = st;
  syncNav();
  if (P.on && START_TAB === 'supps' && !P._started) { P._started = true; setTab('supps'); }
  if (P.on && S.tab === 'today' && S.dayData) renderToday();
}
setInterval(refresh, 400);

function syncNav(){
  const btn = document.querySelector('.nav [data-tab=supps]');
  const inner = document.querySelector('.nav-in');
  if (!P.on) { if (btn) btn.remove(); inner?.classList.remove('has-supps'); return; }
  if (btn) return;
  const b = document.createElement('button');
  b.dataset.tab = 'supps';
  b.innerHTML = `<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M10.6 3.9a5 5 0 017.1 7.1l-6.8 6.8a5 5 0 01-7.1-7.1z"/><path d="M7.2 7.3l7.1 7.1"/></svg>Supps`;
  b.onclick = () => setTab('supps');
  document.querySelector('.nav [data-tab=progress]').before(b);
  inner.classList.add('has-supps');
}

/* ---------- hooks into MyTrack ---------- */
const _setTab = window.setTab;
window.setTab = setTab = function(t){
  if ((t === 'supps' || t === 'suppsadmin') && P.on) {
    S.tab = t;
    document.querySelectorAll('.nav [data-tab]').forEach(b => b.classList.toggle('on', b.dataset.tab === (t === 'suppsadmin' ? 'settings' : t)));
    window.scrollTo(0, 0);
    drawHeader();
    return t === 'supps' ? loadSupps() : renderAdminPage();
  }
  return _setTab(t === 'supps' || t === 'suppsadmin' ? 'today' : t);
};
const _drawHeader = window.drawHeader;
window.drawHeader = drawHeader = function(){
  _drawHeader();
  if (S.tab === 'supps' && P.view !== 'plan') $('#datenav').classList.remove('hidden');
};
const _loadDay = window.loadDay;
window.loadDay = loadDay = async function(){
  if (S.tab === 'supps') { drawHeader(); return loadSupps(); }
  return _loadDay();
};
const _renderToday = window.renderToday;
window.renderToday = renderToday = function(){
  _renderToday();
  const tgt = P.on && P.status?.protein_target;
  if (!tgt || !S.dayData) return;
  const tiles = $('#view').querySelectorAll('.macros .macro');
  if (!tiles.length) return;
  const got = S.dayData.entries.reduce((a, e) => a + (+e.protein || 0), 0);
  tiles[0].innerHTML = `<b>${r0(got)}<small style="font-size:13px;color:var(--muted);font-weight:800"> / ${tgt}g</small></b><span>Protein</span>
    <div class="sp-pbar"><i style="width:${Math.min(100, got / tgt * 100)}%"></i></div>`;
};
const _renderAdmin = window.renderAdmin;
window.renderAdmin = renderAdmin = async function(){
  await _renderAdmin();
  if (!P.on || !S.me?.is_admin || S.tab !== 'admin') return;
  const cards = $('#view').querySelectorAll('.card');
  if (cards.length < 2 || $('#spAdminCard')) return;
  const card = document.createElement('section');
  card.className = 'card'; card.id = 'spAdminCard';
  card.innerHTML = `<h3>Supplements</h3>
    <button class="urow" id="spGoAdmin" style="width:100%"><span class="meal-ic">💊</span><span class="n"><b>Supplement products</b><span>Add products, scan barcodes, see what's in them, choose who has Supps</span></span>${CHEV}</button>`;
  cards[1].after(card);
  $('#spGoAdmin').onclick = () => setTab('suppsadmin');
};

/* ---------- maths ---------- */
function addUp(items, useTaken){
  const out = new Map();
  for (const i of items) {
    if (useTaken && !i.log) continue;
    const q = useTaken ? (i.log.qty ?? i.qty) : i.qty;
    for (const x of (i.product?.nutrients || [])) {
      let e = out.get(x.name);
      if (!e) { e = { name: x.name, unit: x.unit, amount: 0, sources: [] }; out.set(x.name, e); }
      const a = conv(x.amount * q, x.unit, e.unit); if (!(a > 0)) continue;
      e.amount += a;
      const s = e.sources.find(s2 => s2.name === i.name);
      if (s) s.amount += a; else e.sources.push({ name: i.name, amount: a });
    }
  }
  return [...out.values()];
}
function limitOf(e){
  const L = LIMITS[e.name]; if (!L) return null;
  const amt = conv(e.amount, e.unit, L.unit); if (amt == null) return null;
  const pct = amt / L.max * 100;
  return { ...L, amt, pct, level: pct > 100 ? 'over' : pct >= 80 ? 'near' : 'ok' };
}
// 7-day watch list from what was actually ticked (same rules as Stack)
function watchList(ticks, today){
  const byDay = new Map();
  for (const t of ticks) {
    let m = byDay.get(t.day); if (!m) byDay.set(t.day, m = new Map());
    for (const x of t.nutrients) {
      let e = m.get(x.name); if (!e) m.set(x.name, e = { name: x.name, unit: x.unit, amount: 0, sources: [], single: [] });
      const a = conv(x.amount * t.qty, x.unit, e.unit); if (!(a > 0)) continue;
      e.amount += a; e.sources.push(t.name); e.single.push({ name: t.name, mg: conv(x.amount * t.qty, x.unit, 'mg') });
    }
  }
  const days = [...byDay.keys()], names = new Set(days.flatMap(d => [...byDay.get(d).keys()]));
  const flags = [];
  for (const name of names) {
    const per = days.map(d => ({ d, e: byDay.get(d).get(name) })).filter(x => x.e && x.e.amount > 0);
    const te = byDay.get(today)?.get(name);
    const L = LIMITS[name];
    if (L) {
      const amt = e => conv(e.amount, e.unit, L.unit) || 0;
      const tAmt = te ? amt(te) : 0, over = per.filter(x => amt(x.e) > L.max);
      const peak = per.reduce((b, x) => amt(x.e) > (b ? amt(b.e) : -1) ? x : b, null);
      const level = tAmt > L.max ? 'over' : over.length ? 'recent' : tAmt >= L.max * .8 ? 'near' : null;
      if (level) {
        const show = level === 'recent' ? peak : { d: today, e: te };
        flags.push({ name, level, text: level === 'recent'
          ? `Over on ${over.length} of the last ${days.length} days, highest ${nf(amt(peak.e))} ${L.unit} on ${parseISO(peak.d).toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric' })}. Limit ${L.max} ${L.unit} (${L.src}).`
          : `${nf(tAmt)} ${L.unit} today, ${Math.round(tAmt / L.max * 100)}% of the ${L.max} ${L.unit} limit (${L.src}).`,
          from: [...new Set(show.e.sources)].join(', ') });
      }
    }
    if (name === 'Caffeine') {
      const big = per.filter(x => x.e.single.some(s => s.mg > 200));
      if (big.length) {
        const who = [...new Set(big.flatMap(x => x.e.single.filter(s => s.mg > 200).map(s => `${s.name} ${nf(s.mg)} mg`)))];
        flags.push({ name: 'Caffeine in one go', level: big.some(x => x.d === today) ? 'dose' : 'recent',
          text: `${who.join(', ')} in a single serving (EFSA: up to 200 mg at once). Taken like this on ${big.length} of the last ${days.length} days.`, from: '' });
      }
    }
    const rv = UNDER_REVIEW.find(([re]) => re.test(name));
    if (rv) flags.push({ name, level: 'review', text: rv[1], from: [...new Set(per.flatMap(x => x.e.sources))].join(', ') });
  }
  const rank = { over: 0, dose: 1, recent: 2, near: 3, review: 4 };
  return flags.sort((a, b) => rank[a.level] - rank[b.level]);
}

/* ---------- Supps tab ---------- */
async function loadSupps(){
  if (P.view === 'plan') return renderPlan();
  if (!P.data || P.data.day !== S.day) $('#view').innerHTML = '<div class="spin"></div>';
  const day = S.day;
  try {
    const [d, h, t] = await Promise.all([api('/supps/day/' + day), api(`/supps/history?end=${TODAY()}&days=35`),
      api(`/supps/taken?start=${addDays(TODAY(), -6)}&end=${TODAY()}`)]);
    if (S.tab !== 'supps' || S.day !== day) return;
    P.data = d; P.hist = h; P.watch = watchList(t, TODAY());
    renderSupps();
  } catch (e) { if (e.message !== 'auth') $('#view').innerHTML = `<div class="empty"><div class="e">📡</div>${esc(e.message)}</div>`; }
}
function renderSupps(){
  const d = P.data, items = d.slots.flatMap(s => s.items.map(i => ({ ...i, slot: s })));
  const req = items.filter(i => !i.optional), done = req.filter(i => i.log).length;
  const isToday = S.day === TODAY(), nowM = new Date().getHours() * 60 + new Date().getMinutes();
  const next = d.slots.find(s => s.items.some(i => !i.optional && !i.log) && (!isToday || +s.time.slice(0, 2) * 60 + +s.time.slice(3) >= nowM - 60))
    || d.slots.find(s => s.items.some(i => !i.optional && !i.log));
  const hero = `<section class="card hero-card"><div class="hero">
      <div class="ring">${ringSVG(req.length ? done / req.length : 1, false)}<div class="c"><div class="big">${done}<span style="font-size:24px;opacity:.55">/${req.length}</span></div><div class="unit">taken${isToday ? '<br>today' : ''}</div></div></div>
      <div class="hero-stats">
        <div><button class="sp-chip ${d.type}" id="spType" title="Tap to swap">${d.type === 'training' ? '🏋️ Training day' : '🛋️ Rest day'} ⇄</button></div>
        <div class="hs"><div class="k">${next ? 'Next' : 'Done'}</div><div class="v">${next ? esc(next.title) + ` <small>${next.time}</small>` : 'All taken ✅'}</div></div>
        <div class="hs flex"><div class="k">Streak</div><div class="v">${P.hist.streak} <small>day${P.hist.streak === 1 ? '' : 's'} · best ${P.hist.best}</small></div></div>
      </div></div></section>`;
  const seg = `<div class="seg" id="spSeg" style="margin-top:14px">${[['today', isToday ? 'Today' : 'Day'], ['breakdown', 'Breakdown'], ['week', 'Week']].map(([k, l]) => `<button data-k="${k}" class="${P.view === k ? 'on' : ''}">${l}</button>`).join('')}</div>`;
  let body = '';
  if (P.view === 'breakdown') body = breakdownHTML(items);
  else if (P.view === 'week') body = weekHTML();
  else body = todayHTML(d, isToday);
  $('#view').innerHTML = hero + seg + body + `<button class="btn sec" id="spEdit" style="margin-top:18px">✏️ Edit my plan &amp; reminders</button>`;
  $('#spSeg').onclick = e => { const k = e.target.closest('button')?.dataset.k; if (k) { P.view = k; renderSupps(); } };
  $('#spType').onclick = async () => { await api(`/supps/day/${S.day}/type`, { method: 'PUT', body: { type: d.type === 'training' ? 'rest' : 'training' } }); loadSupps(); };
  $('#spEdit').onclick = () => { P.view = 'plan'; drawHeader(); $('#datenav').classList.add('hidden'); renderPlan(); };
  $('#view').querySelectorAll('[data-it]').forEach(r => r.onclick = e => {
    const it = items.find(i => i.id === +r.dataset.it);
    if (e.target.closest('.sp-info')) return openIngredients(it);
    if (it.log) return openLogged(it);
    tick(it);
  });
  $('#view').querySelectorAll('[data-all]').forEach(b => b.onclick = async e => {
    e.stopPropagation();
    const s = d.slots.find(x => x.id === +b.dataset.all);
    await api('/supps/log-all', { method: 'POST', body: { day: S.day, slot_id: s.id, time: S.day === TODAY() ? nowHM() : s.time } });
    buzz(); loadSupps();
  });
  $('#view').querySelectorAll('[data-wi]').forEach(b => b.onclick = () => { const x = P.watchShown[+b.dataset.wi]; openSheet(x.name, `<p style="margin-top:0">${esc(x.text)}</p>${x.from ? `<p class="hint">From: ${esc(x.from)}</p>` : ''}<p class="hint">Upper limits are the most that's considered safe to take every day long term — not targets.</p>`); });
}
async function tick(it){
  const time = S.day === TODAY() ? nowHM() : it.slot.time;
  await api('/supps/log', { method: 'PUT', body: { day: S.day, item_id: it.id, time } });
  buzz();
  if (it.diary && it.product?.macros?.kcal) toast(`Added ${fmt(it.product.macros.kcal * it.qty)} kcal to your diary`);
  loadSupps();
}
function todayHTML(d, isToday){
  P.watchShown = isToday ? P.watch : [];
  const watch = P.watchShown.length ? `<section class="card sp-watch"><h3>Heads up <span class="sub">last 7 days</span></h3>${P.watchShown.map((x, k) => `
      <button class="w" data-wi="${k}" style="width:100%;text-align:left;color:inherit"><span style="font-size:20px">${x.level === 'review' ? '🔎' : '⚠️'}</span><div class="n"><b>${esc(x.name)}</b><span>${esc(x.text)}</span></div>
      <span class="sp-flag ${x.level}">${{ over: 'Over today', dose: 'Today', recent: 'This week', near: 'Close', review: 'Under review' }[x.level]}</span></button>`).join('')}</section>` : '';
  if (!d.slots.length) return watch + `<section class="card"><div class="empty"><div class="e">💊</div>Nothing on your plan for this day yet.<br>Tap <b>Edit my plan</b> to add supplements.</div></section>`;
  return watch + d.slots.map(s => {
    const n = s.items.filter(i => i.log).length, left = s.items.filter(i => !i.log && !i.optional).length;
    return `<section class="card"><div class="meal-h"><div class="meal-ic sp-time">${s.time}</div>
      <div class="t"><b>${esc(s.title)}</b><span>${n} of ${s.items.length} taken${s.note ? ' · ' + esc(s.note) : ''}</span></div>
      ${left > 1 ? `<button class="mini-btn" data-all="${s.id}" style="background:var(--sky);color:var(--blue)">Log all</button>` : ''}</div>
      ${s.items.map(i => `<div class="entry sp-row ${i.log ? 'done' : ''}" data-it="${i.id}">
        <span class="sp-tick ${i.log ? 'on' : ''}">${i.log ? TICK : ''}</span>
        <div class="n"><b>${esc(i.name)}</b><span>${i.optional ? '<span class="sp-opt" style="margin:0 4px 0 0">optional</span>' : ''}${i.diary ? '<span class="sp-opt" style="margin:0 4px 0 0">+ diary</span>' : ''}${esc(i.dose || (i.qty + ' × ' + (i.product?.unit || 'serving')))}</span></div>
        ${i.log ? `<span class="kc">${i.log.time}</span>` : ''}
        ${i.product?.nutrients?.length || i.product?.macros?.kcal ? `<span class="sp-info" title="What's in it">${i.product.nutrients.length || 'i'}</span>` : ''}</div>`).join('')}
    </section>`;
  }).join('');
}
function breakdownHTML(items){
  const planned = addUp(items, false), taken = new Map(addUp(items, true).map(e => [e.name, e]));
  if (!planned.length) return `<section class="card"><div class="empty">No ingredient details on today's plan yet. Add them to products in Admin.</div></section>`;
  planned.sort((a, b) => (limitOf(b)?.pct || 0) - (limitOf(a)?.pct || 0) || a.name.localeCompare(b.name));
  const row = e => { const L = limitOf(e), t = taken.get(e.name), ta = t ? conv(t.amount, t.unit, e.unit) : 0;
    return `<div class="sp-n"><div class="t2"><b>${esc(e.name)}</b><span class="a">${nf(ta)} <span>/ ${nf(e.amount)} ${e.unit}</span></span></div>
      ${L ? `<div class="sp-bar ${L.level}"><i style="width:${Math.min(100, L.pct)}%"></i><span class="lim" style="left:calc(${Math.min(100, 10000 / Math.max(100, L.pct))}% - 2px)"></span></div>
        <div class="s">Full plan is ${Math.round(L.pct)}% of the ${L.max} ${L.unit} upper limit · ${e.sources.map(s => esc(s.name)).join(', ')}</div>`
      : `<div class="s">${e.sources.map(s => esc(s.name)).join(', ')}</div>`}</div>`; };
  const vit = planned.filter(e => VIT_RE.test(e.name)), other = planned.filter(e => !VIT_RE.test(e.name));
  return `<section class="card"><h3>${S.day === TODAY() ? "Today's" : 'This day’s'} plan, added up <span class="sub">taken / planned</span></h3>
      <p class="hint" style="margin-top:-4px">Every active ingredient across the ${items.length} items on the plan. Bars show how close the whole plan gets to the daily upper limit; the line marks the limit.</p></section>
    ${vit.length ? `<section class="card"><h3>Vitamins &amp; minerals <span class="sub">${vit.length}</span></h3>${vit.map(row).join('')}</section>` : ''}
    ${other.length ? `<section class="card"><h3>Other actives <span class="sub">${other.length}</span></h3>${other.map(row).join('')}</section>` : ''}`;
}
function weekHTML(){
  const h = P.hist, last7 = h.days.slice(-7), tracked = h.days.filter(x => x.tracked && x.required);
  const pct = x => x.required ? Math.min(100, x.taken / x.required * 100) : 0;
  const avg = arr => arr.length ? Math.round(arr.reduce((a, x) => a + pct(x), 0) / arr.length) : 0;
  const bars = last7.map(x => `<div><i class="${x.day === TODAY() ? 'today' : x.tracked ? '' : 'none'}" style="height:${x.tracked ? Math.max(4, pct(x)) : 4}%"></i><span>${DAYS[parseISO(x.day).getDay()][0]}</span></div>`).join('');
  const first = parseISO(h.days[0].day).getDay(), pre = (first + 6) % 7;
  const heat = ['M', 'T', 'W', 'T', 'F', 'S', 'S'].map(l => `<b>${l}</b>`).join('') + '<i class="fut" style="visibility:hidden"></i>'.repeat(pre) +
    h.days.map(x => { const p = pct(x); return `<i title="${x.day}: ${x.taken}/${x.required}" class="${!x.tracked ? '' : p >= 100 ? 'l3' : p >= 70 ? 'l2' : p > 0 ? 'l1' : ''}"></i>`; }).join('');
  const missed = h.items.filter(i => !i.optional && i.due >= 2 && i.taken < i.due).slice(0, 5);
  const late = h.items.filter(i => i.avg_time && Math.abs(toM(i.avg_time) - toM(i.planned)) >= 45).slice(0, 4);
  return `<section class="card"><h3>Last 7 days <span class="sub">${avg(last7.filter(x => x.tracked && x.required))}% taken</span></h3><div class="sp-weekbars">${bars}</div>
      <div class="stat3" style="margin-top:14px"><div><b>${h.streak}</b><span>Streak</span></div><div><b>${h.best}</b><span>Best</span></div><div><b>${avg(tracked.slice(-30))}%</b><span>30 days</span></div></div></section>
    <section class="card"><h3>5 weeks</h3><div class="sp-heat">${heat}</div><p class="hint">Darker = more of the plan taken. Optional items never count against you.</p></section>
    ${missed.length ? `<section class="card"><h3>Most missed</h3>${missed.map(i => `<div class="entry"><div class="n"><b>${esc(i.name)}</b><span>${esc(i.slot)} · taken ${i.taken} of ${i.due} days</span></div><span class="pill ghost">${Math.round(i.taken / i.due * 100)}%</span></div>`).join('')}</section>` : ''}
    ${late.length ? `<section class="card"><h3>Timing</h3>${late.map(i => `<div class="entry"><div class="n"><b>${esc(i.name)}</b><span>Planned ${i.planned} · you usually take it at ${i.avg_time}</span></div></div>`).join('')}</section>` : ''}`;
}
const toM = hm => +hm.slice(0, 2) * 60 + +hm.slice(3);

/* ---------- sheets on the Supps tab ---------- */
function openIngredients(it){
  const p = it.product, q = it.log ? (it.log.qty ?? it.qty) : it.qty, m = p?.macros || {};
  openSheet(it.name, `
    <p class="hint" style="margin-top:0">${esc(pname(p))}${p?.serving_text ? ' · label serving ' + esc(p.serving_text) : ''}<br>Your dose: <b>${esc(it.dose || '')}</b> (${q} × ${esc(p?.unit || 'serving')})</p>
    ${m.kcal || m.protein ? `<div class="sp-mac"><div><b>${nf((m.kcal || 0) * q)}</b><span>kcal</span></div><div><b>${nf((m.protein || 0) * q)}g</b><span>Protein</span></div><div><b>${nf((m.carbs || 0) * q)}g</b><span>Carbs</span></div><div><b>${nf((m.fat || 0) * q)}g</b><span>Fat</span></div></div>
      <p class="hint">${it.diary ? 'Ticking this adds its calories to your food diary.' : "Not counted in your food diary — switch that on in Edit my plan."}</p>` : ''}
    ${p?.nutrients?.length ? `<h3 style="margin:16px 0 4px;font-weight:900">What's in your dose</h3>
      <div>${p.nutrients.map(x => { const L = limitOf({ name: x.name, unit: x.unit, amount: x.amount * q });
        return `<div class="sp-n"><div class="t2"><b>${esc(x.name)}</b><span class="a">${nf(x.amount * q)} ${x.unit}</span></div>
          <div class="s">${x.label && x.label !== x.name ? esc(x.label) : ''}${x.nrv ? `${x.label && x.label !== x.name ? ' · ' : ''}<b>${Math.round(x.nrv * q)}% NRV</b>` : ''}</div>
          ${L ? `<div class="sp-bar ${L.level}"><i style="width:${Math.min(100, L.pct)}%"></i></div><div class="s">${Math.round(L.pct)}% of the ${L.max} ${L.unit} daily upper limit from this alone</div>` : ''}</div>`; }).join('')}</div>`
      : '<p class="hint">No ingredient details for this product yet. An admin can add them under Me → Admin → Supplement products.</p>'}
    ${it.log ? '' : '<button class="btn" id="spTickNow">Mark as taken</button>'}`, { full: !!p?.nutrients?.length });
  const b = $('#spTickNow'); if (b) b.onclick = () => { closeSheet(); tick(it); };
}
function openLogged(it){
  openSheet(it.name, `
    <div class="sp-grid2"><div class="field" style="margin-top:0"><label>Taken at</label><input type="time" id="spT" value="${it.log.time}"></div>
      <div class="field" style="margin-top:0"><label>Amount (${esc(it.product?.unit || 'serving')})</label><input type="number" id="spQ" inputmode="decimal" step="0.25" min="0.25" value="${it.log.qty ?? it.qty}"></div></div>
    <button class="btn" id="spSave">Save</button>
    <div class="btn-row"><button class="btn sec" id="spInfo">What's in it</button><button class="btn danger" id="spUn">Not taken</button></div>`);
  $('#spSave').onclick = async () => { try { await api('/supps/log', { method: 'PUT', body: { day: S.day, item_id: it.id, time: $('#spT').value, qty: +$('#spQ').value || it.qty } }); closeSheet(); loadSupps(); } catch (e) { toast(e.message); } };
  $('#spUn').onclick = async () => { await api('/supps/unlog', { method: 'POST', body: { day: S.day, item_id: it.id } }); closeSheet(); loadSupps(); };
  $('#spInfo').onclick = () => openIngredients(it);
}

/* ---------- plan editor (own plan) ---------- */
async function renderPlan(){
  $('#datenav').classList.add('hidden');
  $('#view').innerHTML = '<div class="spin"></div>';
  const [pl, st] = await Promise.all([api('/supps/plan'), api('/supps/status')]);
  P.plan = pl; P.status = st;
  if (S.tab !== 'supps' || P.view !== 'plan') return;
  $('#view').innerHTML = `
    <section class="card"><h3>My plan <button class="mini-btn" id="spBack" style="background:var(--sky);color:var(--blue)">‹ Done</button></h3>
      <p class="hint" style="margin-top:-4px">Changes apply from today, so your history and streaks stay as they were.</p></section>
    ${pl.slots.map(s => `<section class="card"><div class="meal-h"><div class="meal-ic sp-time">${s.time}</div>
        <div class="t"><b>${esc(s.title)}</b><span>${WHEN[s.day_when]}${s.remind && st.remind ? ' · 🔔' : ''}</span></div>
        <button class="mini-btn" data-es="${s.id}" style="background:var(--sky);color:var(--blue)">Edit</button></div>
      ${s.items.map(i => `<button class="entry" data-ei="${i.id}" style="width:100%;text-align:left;color:inherit"><span class="ph" style="font-size:20px">${icon(i.product)}</span>
        <div class="n"><b>${esc(i.name)}</b><span>${i.optional ? '<span class="sp-opt" style="margin:0 4px 0 0">optional</span>' : ''}${i.diary ? '<span class="sp-opt" style="margin:0 4px 0 0">+ diary</span>' : ''}${esc(i.dose || '')}${i.dose ? ' · ' : ''}${i.qty} × ${esc(i.product?.unit || 'serving')}</span></div>${CHEV}</button>`).join('')}
      <button class="add-row" data-ai="${s.id}"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>Add to ${esc(s.title.toLowerCase())}</button>
    </section>`).join('')}
    <button class="btn sec" id="spAddSlot">+ Add a time slot</button>
    <section class="card"><h3>Settings</h3>
      <div class="sp-tg" id="spRem"><div>Phone reminders<small>A nudge at each slot's time if something's still unticked. Needs notifications on (Me → Notifications).</small></div><span class="sp-sw ${st.remind ? 'on' : ''}"></span></div>
      <div class="field"><label>Daily protein target (g)</label><input type="number" id="spPT" inputmode="numeric" value="${st.protein_target || ''}" placeholder="Off"></div>
      <p class="hint">Shows on your Today card as protein so far / target. Leave empty to hide.</p>
      <div class="field"><label>Training days</label><div class="sp-days" id="spTD">${[1, 2, 3, 4, 5, 6, 0].map(d => `<button data-d="${d}" class="${st.training_days.includes(d) ? 'on' : ''}">${DAYS[d]}</button>`).join('')}</div></div>
      <p class="hint">Training-day and rest-day slots switch automatically. Swap any single day with the chip on the Supps tab.</p>
    </section>`;
  $('#spBack').onclick = () => { P.view = 'today'; drawHeader(); loadSupps(); };
  $('#spAddSlot').onclick = () => openSlot(null);
  $('#view').querySelectorAll('[data-es]').forEach(b => b.onclick = () => openSlot(pl.slots.find(s => s.id === +b.dataset.es)));
  $('#view').querySelectorAll('[data-ai]').forEach(b => b.onclick = () => openItem(null, +b.dataset.ai));
  $('#view').querySelectorAll('[data-ei]').forEach(b => b.onclick = () => openItem(pl.slots.flatMap(s => s.items).find(i => i.id === +b.dataset.ei)));
  $('#spRem').onclick = async () => { await api('/supps/prefs', { method: 'PUT', body: { remind: !st.remind } }); renderPlan(); };
  $('#spPT').onchange = async e => { P.status = await api('/supps/prefs', { method: 'PUT', body: { protein_target: +e.target.value || null } }); toast('Saved'); };
  $('#spTD').onclick = async e => { const b = e.target.closest('button'); if (!b) return; b.classList.toggle('on');
    const days = [...$('#spTD').querySelectorAll('.on')].map(x => +x.dataset.d);
    P.status = await api('/supps/prefs', { method: 'PUT', body: { training_days: days } }); };
}
function openSlot(s){
  const isNew = !s;
  openSheet(isNew ? 'New time slot' : 'Edit ' + s.title, `
    <div class="field" style="margin-top:0"><label>Name</label><input id="slN" value="${esc(s?.title || '')}" placeholder="e.g. Breakfast"></div>
    <div class="sp-grid2"><div class="field"><label>Time</label><input type="time" id="slT" value="${s?.time || '08:00'}"></div>
      <div class="field"><label>Days</label><select id="slW">${Object.entries(WHEN).map(([k, l]) => `<option value="${k}" ${(s?.day_when || 'all') === k ? 'selected' : ''}>${l}</option>`).join('')}</select></div></div>
    <div class="field"><label>Note (optional)</label><input id="slNo" value="${esc(s?.note || '')}" placeholder="e.g. With food"></div>
    <div class="sp-tg" id="slR"><div>Remind me at this time</div><span class="sp-sw ${!s || s.remind ? 'on' : ''}"></span></div>
    <button class="btn" id="slSave">${isNew ? 'Add slot' : 'Save'}</button>
    ${isNew ? '' : '<button class="btn danger" id="slDel">Remove this slot</button>'}`);
  $('#slR').onclick = () => $('#slR .sp-sw').classList.toggle('on');
  $('#slSave').onclick = async () => {
    const body = { title: $('#slN').value, time: $('#slT').value, day_when: $('#slW').value, note: $('#slNo').value, remind: $('#slR .sp-sw').classList.contains('on') };
    try { await api(isNew ? '/supps/slots' : '/supps/slots/' + s.id, { method: isNew ? 'POST' : 'PUT', body }); closeSheet(); renderPlan(); } catch (e) { toast(e.message); }
  };
  const del = $('#slDel'); if (del) del.onclick = async () => { await api('/supps/slots/' + s.id, { method: 'DELETE' }); closeSheet(); renderPlan(); };
}
async function openItem(it, slotId){
  const isNew = !it;
  const prods = await api('/supps/products');
  let pid = it?.product_id || null;
  const slots = P.plan.slots;
  const draw = () => {
    const p = prods.find(x => x.id === pid);
    const cal = p && (p.macros?.kcal || p.macros?.protein);
    openSheet(isNew ? 'Add to plan' : 'Edit ' + it.name, `
      <div class="field" style="margin-top:0"><label>Product</label>
        <button class="sp-p" id="itPick" style="border:2px solid var(--line);border-radius:14px;padding:10px 12px">${p ? `<span class="ph">${icon(p)}</span><span class="n"><b>${esc(pname(p))}</b><span>${p.nutrients.length} actives · per ${esc(p.unit)}</span></span>` : '<span class="ph">➕</span><span class="n"><b>Choose a product</b><span>From the supplement library</span></span>'}${CHEV}</button></div>
      <div class="field"><label>Name on your plan</label><input id="itN" value="${esc(it?.name || p?.name || '')}" placeholder="e.g. Omega-3"></div>
      <div class="sp-grid2"><div class="field"><label>How many (${esc(p?.unit || 'serving')}s)</label><input type="number" id="itQ" inputmode="decimal" step="0.25" min="0.25" value="${it?.qty || 1}"></div>
        <div class="field"><label>Time slot</label><select id="itS">${slots.map(s => `<option value="${s.id}" ${(it?.slot_id || slotId) === s.id ? 'selected' : ''}>${s.time} ${esc(s.title)}</option>`).join('')}</select></div></div>
      <div class="field"><label>Dose note</label><input id="itD" value="${esc(it?.dose || '')}" placeholder="e.g. 1 softgel with a meal"></div>
      <div class="sp-tg" id="itO"><div>Optional<small>Never counts against your streak</small></div><span class="sp-sw ${it?.optional ? 'on' : ''}"></span></div>
      ${cal ? `<div class="sp-tg" id="itC"><div>Count calories in my food diary<small>${nf(p.macros.kcal || 0)} kcal · ${nf(p.macros.protein || 0)}g protein per ${esc(p.unit)}</small></div><span class="sp-sw ${(it ? it.diary : true) ? 'on' : ''}"></span></div>` : ''}
      <button class="btn" id="itSave">${isNew ? 'Add to plan' : 'Save'}</button>
      ${isNew ? '' : '<button class="btn danger" id="itDel">Remove from plan</button>'}`);
    ['itO', 'itC'].forEach(k => { const el = $('#' + k); if (el) el.onclick = () => el.querySelector('.sp-sw').classList.toggle('on'); });
    $('#itPick').onclick = () => pickProduct(prods, id => { pid = id; draw(); });
    $('#itSave').onclick = async () => {
      const body = { slot_id: +$('#itS').value, product_id: pid, name: $('#itN').value.trim() || p?.name || '', dose: $('#itD').value,
        qty: +$('#itQ').value || 1, optional: $('#itO .sp-sw').classList.contains('on'), diary: !!$('#itC .sp-sw')?.classList.contains('on') };
      try { await api(isNew ? '/supps/items' : '/supps/items/' + it.id, { method: isNew ? 'POST' : 'PUT', body }); closeSheet(); renderPlan(); } catch (e) { toast(e.message); }
    };
    const del = $('#itDel'); if (del) del.onclick = async () => { await api('/supps/items/' + it.id, { method: 'DELETE' }); closeSheet(); renderPlan(); };
  };
  draw();
}
function pickProduct(prods, done){
  const list = q => prods.filter(p => !q || (p.name + ' ' + p.brand).toLowerCase().includes(q.toLowerCase()));
  openSheet('Choose a product', `<div class="search" style="box-shadow:none;border:2px solid var(--line);margin-top:0"><input id="ppQ" placeholder="Search products" autocomplete="off"></div><div id="ppL" style="margin-top:8px"></div>
    <p class="hint">Not there? ${S.me.is_admin ? 'Add it in Me → Admin → Supplement products.' : 'Ask an admin to add it.'}</p>`, { full: true });
  const draw = () => { $('#ppL').innerHTML = list($('#ppQ').value).map(p => `<button class="sp-p" data-p="${p.id}"><span class="ph">${icon(p)}</span><span class="n"><b>${esc(pname(p))}</b><span>${p.nutrients.length} actives · per ${esc(p.unit)}</span></span>${CHEV}</button>`).join('') || '<div class="empty">No matches</div>';
    $('#ppL').querySelectorAll('[data-p]').forEach(b => b.onclick = () => done(+b.dataset.p)); };
  $('#ppQ').oninput = draw; draw();
}

/* ---------- Admin: supplement products & who has Supps ---------- */
async function renderAdminPage(){
  if (!S.me?.is_admin) return setTab('settings');
  $('#datenav').classList.add('hidden');
  $('#view').innerHTML = '<div class="spin"></div>';
  const [acc, prods] = await Promise.all([api('/admin/supps/access'), api('/supps/products?q=' + encodeURIComponent(P.adminQ))]);
  if (S.tab !== 'suppsadmin') return;
  const actives = new Set(prods.flatMap(p => p.nutrients.map(n => n.name))).size;
  $('#view').innerHTML = `
    <section class="card"><h3>Supplement products <button class="mini-btn" id="saBack" style="background:var(--sky);color:var(--blue)">‹ Admin</button></h3>
      <div class="stat3"><div><b>${acc.stats.products}</b><span>Products</span></div><div><b>${acc.stats.items}</b><span>On plans</span></div><div><b>${actives}</b><span>Actives</span></div></div></section>
    <section class="card"><h3>Who has Supplements</h3>
      ${acc.users.map(u => `<div class="sp-tg" data-acc="${u.id}">${avatar(u, 'sm')}<div style="flex:1">${esc(u.name)}<small>${u.enabled ? 'Supps tab on' : 'Off — their MyTrack has no Supps tab'}</small></div><span class="sp-sw ${u.enabled ? 'on' : ''}"></span></div>`).join('')}
      <p class="hint">The product library is shared; each person with Supplements has their own plan and history.</p></section>
    <section class="card"><h3>Products <button class="mini-btn" id="saAdd">+ Add product</button></h3>
      <div class="search" style="box-shadow:none;border:2px solid var(--line);margin:0 0 6px"><input id="saQ" placeholder="Search products or barcode" value="${esc(P.adminQ)}" autocomplete="off"></div>
      <div>${prods.map(p => `<button class="sp-p" data-p="${p.id}"><span class="ph">${icon(p)}</span>
        <span class="n"><b>${esc(pname(p))}</b><span>${p.nutrients.length} actives · per ${esc(p.unit)}${p.macros?.kcal ? ' · ' + nf(p.macros.kcal) + ' kcal' : ''}${p.on_plan ? ' · on your plan' : ''}</span></span>${CHEV}</button>`).join('') || '<div class="empty">No products yet</div>'}</div></section>`;
  $('#saBack').onclick = () => setTab('admin');
  $('#saAdd').onclick = () => openProduct(null);
  let tq; $('#saQ').oninput = e => { clearTimeout(tq); P.adminQ = e.target.value; tq = setTimeout(renderAdminPage, 400); };
  $('#view').querySelectorAll('[data-p]').forEach(b => b.onclick = () => openProduct(prods.find(p => p.id === +b.dataset.p)));
  $('#view').querySelectorAll('[data-acc]').forEach(b => b.onclick = async () => {
    const u = acc.users.find(x => x.id === +b.dataset.acc);
    try { await api('/admin/supps/access', { method: 'PUT', body: { user_id: u.id, enabled: !u.enabled } }); renderAdminPage(); } catch (e) { toast(e.message); }
  });
}
function openProduct(p, prefill){
  const isNew = !p;
  const v = { name: '', brand: '', barcode: '', serving_text: '', unit: 'capsule', macros: {}, nutrients: [], image_url: '', ...(p || {}), ...(prefill || {}) };
  if (!v.nutrients.length) v.nutrients = [{ name: '', amount: '', unit: 'mg' }];
  const macroKeys = [['kcal', 'kcal'], ['protein', 'Protein g'], ['carbs', 'Carbs g'], ['fat', 'Fat g'], ['sugar', 'Sugar g'], ['satfat', 'Sat fat g']];
  openSheet(isNew ? 'Add a product' : 'Edit product', `
    <button class="btn sec" id="prScan" style="margin-top:0">📷 Scan barcode</button>
    <div id="prScanBox"></div>
    <p class="hint">Fills the name, brand and any calories from Open Food Facts. Vitamins and actives usually aren't in there, so add those from the label below.</p>
    <div class="field"><label>Product name</label><input id="prN" value="${esc(v.name)}" placeholder="e.g. Magnesium Glycinate"></div>
    <div class="sp-grid2"><div class="field"><label>Brand</label><input id="prB" value="${esc(v.brand)}" placeholder="e.g. Zapply"></div>
      <div class="field"><label>Barcode</label><input id="prC" value="${esc(v.barcode || '')}" inputmode="numeric" placeholder="Optional"></div></div>
    <div class="sp-grid2"><div class="field"><label>Amounts below are per</label><input id="prU" value="${esc(v.unit)}" placeholder="capsule / scoop / shot"></div>
      <div class="field"><label>Label serving</label><input id="prS" value="${esc(v.serving_text)}" placeholder="e.g. 2 capsules"></div></div>
    <div class="field"><label>What's in one <span id="prUL">${esc(v.unit || 'serving')}</span></label>
      <div id="prNut">${v.nutrients.map(nutRow).join('')}</div>
      <button class="add-row" id="prAddN" style="margin-top:8px">+ Add ingredient</button></div>
    <div class="sp-tg" id="prHasC"><div>Has calories<small>Shakes, shots, gummies</small></div><span class="sp-sw ${Object.keys(v.macros || {}).length ? 'on' : ''}"></span></div>
    <div id="prMac" style="${Object.keys(v.macros || {}).length ? '' : 'display:none'}"><div class="sp-grid2">${macroKeys.map(([k, l]) => `<div class="field"><label>${l}</label><input data-m="${k}" type="number" inputmode="decimal" step="any" value="${v.macros?.[k] ?? ''}"></div>`).join('')}</div>
      <p class="hint">Per one <span class="prUL2">${esc(v.unit || 'serving')}</span>.</p></div>
    <button class="btn" id="prSave">${isNew ? 'Save product' : 'Save'}</button>
    ${isNew ? '' : '<button class="btn danger" id="prDel">Delete product</button>'}`, { full: true });
  $('#prU').oninput = e => { $('#prUL').textContent = e.target.value || 'serving'; document.querySelectorAll('.prUL2').forEach(x => x.textContent = e.target.value || 'serving'); };
  $('#prAddN').onclick = () => { $('#prNut').insertAdjacentHTML('beforeend', nutRow({ name: '', amount: '', unit: 'mg' })); };
  $('#prNut').onclick = e => { if (e.target.closest('.rm')) e.target.closest('.sp-ned').remove(); };
  $('#prHasC').onclick = () => { const s = $('#prHasC .sp-sw'); s.classList.toggle('on'); $('#prMac').style.display = s.classList.contains('on') ? '' : 'none'; };
  $('#prScan').onclick = () => scanInto(code => lookupInto(code, p));
  $('#prSave').onclick = async () => {
    const nutrients = [...$('#prNut').querySelectorAll('.sp-ned')].map(r => ({ name: r.children[0].value.trim(), amount: r.children[1].value, unit: r.children[2].value })).filter(n => n.name && n.amount !== '');
    const macros = {}; if ($('#prHasC .sp-sw').classList.contains('on')) document.querySelectorAll('#prMac [data-m]').forEach(i => { if (i.value !== '') macros[i.dataset.m] = +i.value; });
    const body = { name: $('#prN').value, brand: $('#prB').value, barcode: $('#prC').value, unit: $('#prU').value || 'serving', serving_text: $('#prS').value, macros, nutrients, image_url: v.image_url || '' };
    try { await api(isNew ? '/admin/supps/products' : '/admin/supps/products/' + p.id, { method: isNew ? 'POST' : 'PUT', body }); closeSheet(); toast('Product saved'); if (S.tab === 'suppsadmin') renderAdminPage(); } catch (e) { toast(e.message); }
  };
  const del = $('#prDel'); if (del) del.onclick = async () => { await api('/admin/supps/products/' + p.id, { method: 'DELETE' }); closeSheet(); renderAdminPage(); };
}
function nutRow(n){
  return `<div class="sp-ned"><input value="${esc(n.label || n.name)}" placeholder="Ingredient"><input value="${n.amount === '' ? '' : nf(n.amount)}" placeholder="0" inputmode="decimal">
    <select>${['mg', 'mcg', 'g', 'iu'].map(u => `<option value="${u}" ${u === n.unit ? 'selected' : ''}>${u === 'iu' ? 'IU' : u}</option>`).join('')}</select><button class="rm" type="button" aria-label="Remove">×</button></div>`;
}
// Uses MyTrack's own scanner library in a small box inside the product sheet
async function scanInto(onCode){
  const box = $('#prScanBox');
  box.innerHTML = `<div class="scan-wrap" style="margin-top:10px"><div id="spReader"></div></div><div class="sp-grid2" style="margin-top:8px"><input class="sp-in" id="spCode" inputmode="numeric" placeholder="Or type the barcode"><button class="btn" id="spGo" style="margin:0">Look up</button></div>`;
  $('#spGo').onclick = () => { const c = $('#spCode').value.replace(/\D/g, ''); if (c) { stopSpScan(); onCode(c); } };
  if (!window.isSecureContext) return;
  for (let i = 0; i < 40 && !window.Html5Qrcode; i++) await new Promise(r => setTimeout(r, 100));
  if (!window.Html5Qrcode) return;
  try {
    const F = Html5QrcodeSupportedFormats;
    P.scanner = new Html5Qrcode('spReader', { formatsToSupport: [F.EAN_13, F.EAN_8, F.UPC_A, F.UPC_E, F.CODE_128], verbose: false, experimentalFeatures: { useBarCodeDetectorIfSupported: true } });
    await P.scanner.start({ facingMode: 'environment' }, { fps: 12, qrbox: (w, h) => ({ width: Math.floor(w * .8), height: Math.floor(h * .4) }), disableFlip: true },
      code => { buzz(); stopSpScan(); onCode(code); }, () => {});
  } catch {}
}
function stopSpScan(){ if (P.scanner) { const s = P.scanner; P.scanner = null; try { s.stop().then(() => s.clear()).catch(() => {}); } catch {} } }
const _closeScanner = window.closeScanner;   // MyTrack calls this whenever a sheet opens or closes
window.closeScanner = closeScanner = function(){ stopSpScan(); return _closeScanner(); };
async function lookupInto(code, p){
  $('#prScanBox').innerHTML = `<div class="spin"></div>`;
  try {
    const r = await api('/admin/supps/barcode/' + encodeURIComponent(code));
    if (r.existing) { toast('Already in the library'); return openProduct(r.existing); }
    if (!r.found) { $('#prScanBox').innerHTML = `<p class="hint">Not found on Open Food Facts — fill it in from the label. Barcode saved.</p>`; $('#prC').value = code; return; }
    const f = r.product, cur = collect();
    openProduct(p, { ...cur, name: cur.name || f.name, brand: cur.brand || f.brand, barcode: f.barcode, serving_text: cur.serving_text || f.serving_text,
      macros: Object.keys(f.macros || {}).length ? f.macros : cur.macros, image_url: f.image_url });
    toast(Object.keys(f.macros || {}).length ? 'Found — calories filled in per serving' : 'Found — add the ingredients from the label');
  } catch (e) { $('#prScanBox').innerHTML = `<p class="hint">${esc(e.message)}</p>`; }
}
function collect(){
  return { name: $('#prN').value, brand: $('#prB').value, barcode: $('#prC').value, unit: $('#prU').value, serving_text: $('#prS').value,
    nutrients: [...$('#prNut').querySelectorAll('.sp-ned')].map(r => ({ name: r.children[0].value, amount: r.children[1].value, unit: r.children[2].value })).filter(n => n.name),
    macros: {} };
}
})();
