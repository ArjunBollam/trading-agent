"use strict";

// paperbot's web page. Reads the JSON API in paperbot/web/server.py; every change is a POST with the page's token.

let TOKEN = window.PAPERBOT_TOKEN; // renewed from /api/token when the server has restarted
const ET = "America/New_York";
const POLL_MS = 5000;

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (x, d = 2) => (x == null ? "–" : Number(x).toFixed(d));
const money = (x) => (x == null ? "–" : (x < 0 ? "-" : "+") + "$" + Math.abs(x).toLocaleString("en-US", { maximumFractionDigits: 0 }));
const pct = (x) => (x == null ? "–" : (x >= 0 ? "+" : "") + Number(x).toFixed(1) + "%");
const tone = (x) => (x == null || x === 0 ? "" : x > 0 ? "up" : "down");
const etTime = (d, seconds = false) =>
  d.toLocaleTimeString("en-US", { timeZone: ET, hour: "2-digit", minute: "2-digit", second: seconds ? "2-digit" : undefined, hour12: false });
const hm = (iso) => (iso ? etTime(new Date(iso)) : "");
const etDate = (t) => new Date(t * 1000).toLocaleDateString("en-US", { timeZone: ET });
// a candle's time, with its day when that isn't today's
const when = (t) => (etDate(t) === etDate(Date.now() / 1000) ? "" : etDay(new Date(t * 1000)) + " ") + etTime(new Date(t * 1000));
const etDay = (d) => d.toLocaleDateString("en-US", { timeZone: ET, weekday: "short" });
const SESSION_NAMES = { pre: "pre-market", regular: "regular hours", post: "after hours" };

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (opts.body !== undefined) {
    init.method = opts.method || "POST";
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  if (init.method !== "GET") init.headers["X-Paperbot-Token"] = TOKEN;
  const r = await fetch(path, init);
  if (r.status === 403 && init.method !== "GET" && !opts.retried) {
    // the server restarted since this page loaded, so it has a new token: fetch it and try once more
    const t = await fetch("/api/token", { cache: "no-store" }).then((x) => x.json()).catch(() => null);
    if (t && t.token && t.token !== TOKEN) {
      TOKEN = t.token;
      return api(path, { ...opts, retried: true });
    }
  }
  let data = null;
  try { data = await r.json(); } catch { /* not JSON */ }
  if (!r.ok) throw new Error((data && data.error) || `${r.status} ${r.statusText}`);
  return data;
}

let toastTimer;
function toast(msg, isError = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show" + (isError ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = "toast"), isError ? 6000 : 2500);
}

function sideBadge(side) {
  return `<span class="side-badge ${side}">${side === "buy" ? "BUY" : "SELL"}</span>`;
}

/* ---------- hover tooltips: what a signal means, with an example (paperbot/explain.py) ---------- */

let glossary = {};
let glossaryLoading = null;
// loaded once; a failure (say, during a server restart) is retried on the next dashboard refresh
function loadGlossary() {
  if (glossaryLoading || Object.keys(glossary).length) return;
  glossaryLoading = api("/api/glossary").then((g) => { glossary = g || {}; }).catch(() => {})
    .finally(() => { glossaryLoading = null; });
}
loadGlossary();

const tipEl = document.createElement("div");
tipEl.className = "tip";
tipEl.hidden = true;
tipEl.setAttribute("role", "tooltip");
document.body.append(tipEl);

const fmtVol = (v) => (v >= 1e6 ? (v / 1e6).toFixed(2) + "M" : v >= 1e3 ? (v / 1e3).toFixed(1) + "K" : String(Math.round(v)));
const fmtSigned = (v) => (v > 0 ? "+" : v < 0 ? "−" : "") + fmtVol(Math.abs(v));

// show the tooltip beside a point (viewport coordinates), flipped to stay on screen
function placeTip(x, y) {
  tipEl.hidden = false;
  const gap = 14, w = tipEl.offsetWidth, h = tipEl.offsetHeight;
  let left = x + gap, top = y + gap;
  if (left + w > window.innerWidth - 8) left = Math.max(8, x - gap - w);
  if (top + h > window.innerHeight - 8) top = Math.max(8, y - gap - h);
  tipEl.style.left = `${left}px`;
  tipEl.style.top = `${top}px`;
}
function hideTip() { tipEl.hidden = true; }
window.addEventListener("scroll", hideTip, { passive: true });

function explainHtml(kind) {
  const g = glossary[kind];
  if (!g) return "";
  return (g.looks ? `<div class="tip-row"><b>Looks like</b>${esc(g.looks)}</div>` : "") +
    `<div class="tip-row"><b>What it means</b>${esc(g.meaning)}</div>` +
    `<div class="tip-row tip-ex"><b>Example</b>${esc(g.example)}</div>`;
}

function signalTipHtml(s) {
  const flags = [
    s.live ? "live: not confirmed until the candle closes" : "",
    s.weak ? "log only" : "",
    s.against === true ? "against your position" : s.against === false && s.held ? "with your position" : "",
    s.conflict ? "against the bigger picture" : "",
  ].filter(Boolean);
  // on a 1-minute chart, say plainly that it's about the scanner's own candle, and show that candle's prices
  const base = lastState?.bar_minutes;
  const start = Math.floor(new Date(s.time).getTime() / 1000);
  const own = oneMinute() && !s.live ? lastState.symbols?.[s.symbol]?.bars?.find((b) => b[0] === start) : null;
  const span = oneMinute() && !s.live;
  const time = span ? `${base}-minute candle ${hm(s.time)}–${etTime(new Date((start + base * 60) * 1000))} · closed ${num(s.price)}` : `${hm(s.time)} · ${num(s.price)}`;
  const ohlc = own ? `<div class="tip-ohlc">That candle: O ${num(own[1])} · H ${num(own[2])} · L ${num(own[3])} · C ${num(own[4])}</div>` : "";
  let flowText = s.flow_detail || "Order flow " + s.flow;
  if (span) flowText = flowText.replace("on this candle", `on that ${base}-minute candle`).replace(/over the last (\d+) candles/, `over the last $1 ${base}-minute candles`);
  const flow = s.flow ? `<div class="tip-detail flow-${esc(s.flow)}">${esc(flowText)}</div>` : "";
  return `<div class="tip-sig ${s.side}">
    <div class="tip-title">${sideBadge(s.side)}<b>${esc(s.label)}</b><span class="muted">${time}</span></div>
    ${ohlc}
    ${s.detail ? `<div class="tip-detail">${esc(s.detail)}</div>` : ""}
    ${flow}
    ${auditHtml(s.live ? `live|${s.symbol}|${s.level}|${s.side}|${s.ts}` : `scan|${s.symbol}|${s.kind}|${s.level || ""}|${s.time}`)}
    ${flags.length ? `<div class="tip-flags">${flags.map((f) => `<span class="tag">${esc(f)}</span>`).join("")}</div>` : ""}
    ${explainHtml(s.kind)}</div>`;
}

// a candle on a chart: its prices and every signal on it, the marker under the pointer first
function chartTip(card, param) {
  if (!param.point || param.time == null || param.point.x < 0 || param.point.y < 0) return hideTip();
  const bar = param.seriesData.get(card.candles);
  const vol = param.seriesData.get(card.volume);
  const side = param.hoveredObjectId ? String(param.hoveredObjectId).split("|")[1] : null;
  const rank = (s) => (side && s.side !== side ? 2 : 0) + (s.weak ? 1 : 0);
  const sigs = [...(card.sigsAt?.get(param.time) || [])].sort((a, b) => rank(a) - rank(b));
  if (!bar && !sigs.length) return hideTip();
  let html = "";
  if (bar) {
    const chg = bar.open ? ((bar.close - bar.open) / bar.open) * 100 : null;
    html += `<div class="tip-head"><b>${esc(card.el.dataset.sym)}</b><span>${when(param.time)} candle</span>` +
      `<span class="${tone(chg)}">${chg == null ? "" : (chg >= 0 ? "+" : "") + chg.toFixed(2) + "%"}</span></div>` +
      `<div class="tip-ohlc">O ${num(bar.open)} · H ${num(bar.high)} · L ${num(bar.low)} · C ${num(bar.close)}${vol ? ` · Vol ${fmtVol(vol.value)}` : ""}</div>`;
    const fb = card.flowAt?.get(param.time);
    for (const o of card.overlaysAt?.get(param.time) || []) {
      html += `<div class="tip-ohlc"><b class="overlay-tag" style="background:${o.color}">${esc(o.name)}</b> ${esc(o.text)}</div>` +
        (o.source ? auditHtml(`${oneMinute() ? 1 : lastState?.bar_minutes}|${card.el.dataset.sym}|${o.source}|${o.kind}|${o.side}|${o.t}`) : "");
    }
    if (fb) html += `<div class="tip-ohlc">Order flow: delta <b class="${tone(fb[1])}">${fmtSigned(fb[1])}</b> · CVD <b class="${tone(fb[2])}">${fmtSigned(fb[2])}</b></div>`;
  }
  const shown = sigs.slice(0, 3);
  html += shown.map(signalTipHtml).join("");
  if (card.el.classList.contains("expanded") && !sigs.length && !card.overlaysAt?.get(param.time)) return hideTip(); // the legend has the prices
  if (sigs.length > shown.length) html += `<div class="tip-more muted">+${sigs.length - shown.length} more on this candle, listed under Signals today</div>`;
  else if (!sigs.length) html += `<div class="tip-more muted">No signal on this candle.</div>`;
  tipEl.innerHTML = html;
  const r = card.box.getBoundingClientRect();
  placeTip(r.left + param.point.x, r.top + param.point.y);
}

// any signal or pattern name in the lists: hover it to see what it means
document.addEventListener("mouseover", (e) => {
  const el = e.target.closest?.("[data-explain]");
  if (!el || !glossary[el.dataset.explain]) return;
  tipEl.innerHTML = `<div class="tip-head"><b>${esc(glossary[el.dataset.explain].label)}</b></div>${explainHtml(el.dataset.explain)}`;
  const r = el.getBoundingClientRect();
  placeTip(r.left, r.bottom - 8);
});
document.addEventListener("mouseout", (e) => {
  const el = e.target.closest?.("[data-explain]");
  if (el && !el.contains(e.relatedTarget)) hideTip();
});

function effectTag(s) {
  if (s.against === true) return `<span class="tag against">against your position</span>`;
  if (s.against === false && s.held) return `<span class="tag with">with your position</span>`;
  return "";
}

/* ---------- tabs ---------- */

const loaders = {};
function showTab(name) {
  const alone = /^chart\/(.+)$/.exec(name);
  setFocus(alone ? decodeURIComponent(alone[1]).toUpperCase() : null);
  if (alone || !$(`#tab-${name}`)) name = "dashboard";
  $$("#nav button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach((t) => t.classList.toggle("active", t.id === `tab-${name}`));
  const hash = focusSym ? `#chart/${encodeURIComponent(focusSym)}` : `#${name}`;
  if (location.hash !== hash) history.replaceState(null, "", hash);
  loaders[name]?.();
  schedulePoll(name === "dashboard" ? POLL_MS : OVERVIEW_POLL_MS);
}
$$("#nav button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
window.addEventListener("hashchange", () => showTab(location.hash.slice(1)));
const activeTab = () => $("#nav button.active")?.dataset.tab;

/* ---------- top bar ---------- */

function renderPills(ov) {
  if (ov.version && ov.version !== window.PAPERBOT_VERSION) return location.reload(); // the page's code changed
  const sc = ov.scanner;
  const ps = $("#pill-scanner");
  const ago = sc.age_seconds != null ? ` · ${sc.age_seconds < 90 ? sc.age_seconds + "s" : Math.round(sc.age_seconds / 60) + "m"} ago` : "";
  const resumes = sc.resumes ? new Date(sc.resumes) : null;
  const when = resumes ? `${etDay(resumes) === etDay(new Date()) ? "" : etDay(resumes) + " "}${etTime(resumes)}` : "";
  ps.textContent = sc.status === "running" ? `Scanner live${ago}` : sc.status === "idle" ? `Scanner idle · resumes ${when} ET`
    : sc.status === "stale" ? `Scanner stale${ago}` : "Scanner off";
  ps.className = "pill " + (sc.status === "running" ? "ok" : sc.status === "idle" ? "" : sc.status === "stale" ? "warn" : "bad");
  ps.title = sc.status === "idle" ? `Between the sessions it scans (${(sc.sessions || []).map((x) => SESSION_NAMES[x]).join(", ")}); change them in Settings`
    : "The live scanner (launchd job com.paperbot.spy-live)";
  const pm = $("#pill-market");
  pm.textContent = { pre: "Pre-market", regular: "Market open", post: "After hours" }[ov.session] || "Market closed";
  pm.className = "pill " + (ov.session === "regular" ? "ok" : ov.session ? "warn" : "");
  renderAuditPill(ov.audit);
  renderReportAlert(ov.report);
  const h = ov.halts;
  const halted = h.all || h.manual || h.day;
  const ph = $("#pill-halt");
  ph.textContent = h.all ? "Bot halted (all)" : halted ? "Bot: entries halted" : "Bot: entries allowed";
  ph.className = "pill " + (h.all ? "bad" : halted ? "warn" : "ok");
}
setInterval(() => ($("#clock").textContent = etTime(new Date(), true) + " ET"), 1000);

/* ---------- dashboard ---------- */

const cards = {}; // symbol -> { el, chart, candles, volume, levelLines, liveLine, keys }
let seen = null; // signal ids already shown, so new ones flash
let lastSignals = [];
const showOverlay = { qqe: true, sr: true, sqz: true, smc: true, arrows: true }; // which of the overlays are drawn: the boxes above the charts
const showSmc = { structure: true, blocks: true, equal: true, range: true, zones: true, fvg: false }; // the parts of Smart Money
for (const id of Object.keys(showOverlay)) {
  try { showOverlay[id] = localStorage.getItem(id) !== "off"; } catch { /* no storage: shown */ }
}
for (const id of Object.keys(showSmc)) {
  try {
    const saved = localStorage.getItem(`smc-${id}`);
    if (saved) showSmc[id] = saved === "on";
  } catch { /* no storage: the defaults */ }
}
let focusSym = null; // the symbol shown alone, filling the page (#chart/SPY)
let chartTf = "base"; // the charts' candles: "1" for 1 minute, "base" for the scanner's own (5 minutes)
try { if (localStorage.getItem("tf") === "1") chartTf = "1"; } catch { /* no storage: the scanner's own */ }
let lastChart = null; // the 1-minute candles, fetched only while they're shown
let lastState = null;
let liveInfo = null;
let dashboardRefresh = null;
let chartLibraryPromise = null;

function loadChartLibrary() {
  if (window.LightweightCharts) return Promise.resolve();
  if (!chartLibraryPromise) {
    chartLibraryPromise = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js";
      script.async = true;
      script.onload = () => window.LightweightCharts ? resolve() : reject(new Error("Chart library did not initialize"));
      script.onerror = () => reject(new Error("Chart library failed to load"));
      document.head.append(script);
    });
  }
  return chartLibraryPromise;
}

const chartObserver = "IntersectionObserver" in window ? new IntersectionObserver((entries) => {
  for (const entry of entries) {
    if (!entry.isIntersecting) continue;
    chartObserver.unobserve(entry.target);
    const card = cards[entry.target.closest(".card")?.dataset.sym];
    if (card) loadCardChart(card);
  }
}, { rootMargin: "300px 0px" }) : null;

function loadCardChart(card) {
  if (card.chart || card.chartLoading) return;
  card.chartLoading = true;
  loadChartLibrary().then(() => {
    if (!card.el.isConnected || card.chart) return;
    initializeChart(card);
    card.chartLoading = false;
    card.loading.remove();
    updateCard(card, card.el.dataset.sym, lastState?.symbols?.[card.el.dataset.sym]);
  }).catch(() => {
    card.chartLoading = false;
    card.loading.textContent = "Chart unavailable. Check your internet connection.";
    card.loading.classList.add("error");
  });
}

function signalId(s) {
  return `${s.ts}|${s.symbol}|${s.kind}|${s.level}`;
}

async function refreshDashboard() {
  if (dashboardRefresh) return dashboardRefresh;
  dashboardRefresh = (async () => {
    loadGlossary();
    try {
      const [{ overview: ov, state: st, signals: sg }, minutes] = await Promise.all([
        api("/api/dashboard"), chartTf === "1" ? api("/api/chart?minutes=1").catch(() => null) : null]);
      if (minutes) lastChart = minutes;
      renderPills(ov);
      liveInfo = ov.live_symbols;
      lastState = st;
      lastSignals = sg.signals;
      renderChips();
      renderCards();
      renderPositions();
      renderFeed();
    } catch (e) {
      $("#live-note").textContent = "Can't reach the server: " + e.message;
    }
  })();
  try { await dashboardRefresh; } finally { dashboardRefresh = null; }
}

const oneMinute = () => chartTf === "1" && lastState?.bar_minutes !== 1;

// what a card shows: the scanner's own candles, or the 1-minute ones, each with its own RSI, trend, overlays and order flow
// (VWAP, the levels and the bigger picture don't depend on the candles shown)
function chartView(sym, v) {
  if (!v || !oneMinute()) return v;
  const c = lastChart?.symbols?.[sym];
  return { ...v, earlier: [], bars: c?.bars || [], forming: c?.forming || null, qqe: c?.qqe || [], sr: c?.sr || null, sqz: c?.sqz || null, smc: c?.smc || null, flow: c?.flow || null,
    rsi: c?.rsi ?? null, trend: c?.trend ?? null, waiting: !c };
}

function renderTf() {
  const base = lastState?.bar_minutes;
  const el = $("#tf");
  el.hidden = !base || base === 1;
  for (const b of $$("button", el)) {
    if (b.dataset.tf === "base") b.textContent = `${base}m`;
    b.classList.toggle("on", b.dataset.tf === chartTf);
    b.setAttribute("aria-pressed", b.dataset.tf === chartTf);
  }
}

$("#tf").addEventListener("click", (e) => {
  const tf = e.target.closest("button")?.dataset.tf;
  if (!tf || tf === chartTf) return;
  chartTf = tf;
  try { localStorage.setItem("tf", tf); } catch { /* no storage: this page view only */ }
  if (!lastState?.symbols) return;
  renderTf();
  renderCards();
  if (tf === "1") refreshDashboard(); // the 1-minute candles, now rather than at the next poll
});

function renderChips() {
  const syms = liveInfo.symbols;
  const bad = new Set(lastState?.no_data || []);
  const chipsKey = JSON.stringify([syms, [...bad].sort()]);
  if ($("#chips").dataset.key !== chipsKey) {
    $("#chips").dataset.key = chipsKey;
    $("#chips").innerHTML = syms.length
      ? syms.map((s) => `<span class="chip${bad.has(s) ? " bad" : ""}" title="${bad.has(s) ? "No data from Robinhood: check the ticker" : ""}">${esc(s)}<button title="Stop scanning ${esc(s)}" data-remove="${esc(s)}">×</button></span>`).join("")
      : `<span class="muted">none set: everything you hold</span>`;
  }
  renderTf();
  const st = lastState;
  $("#live-note").textContent =
    st.status === "off" ? "The live scanner hasn't written anything yet." :
    `Signals on ${st.bar_minutes}-minute candles, live price every ${st.tick_seconds || "–"} s · updated ${hm(st.updated)}` +
    (liveInfo.source === "file" && liveInfo.by ? ` · list changed by ${liveInfo.by}` : "");
}

for (const [group, prefix] of [[showOverlay, ""], [showSmc, "smc-"]]) {
  for (const id of Object.keys(group)) {
    const box = $(`#${prefix}${id}-on`);
    box.checked = group[id];
    box.addEventListener("change", () => {
      group[id] = box.checked;
      try { localStorage.setItem(prefix + id, box.checked ? "on" : "off"); } catch { /* no storage: this page view only */ }
      if (lastState) renderCards();
    });
  }
}
document.addEventListener("click", (e) => { // the parts menu closes on a click anywhere else
  if (!e.target.closest("#smc-parts")) $("#smc-parts").open = false;
});
$("#chips").addEventListener("click", async (e) => {
  const sym = e.target.dataset.remove;
  if (!sym) return;
  try {
    liveInfo = await api("/api/live-symbols", { body: { action: "remove", symbols: [sym] } });
    renderChips();
    toast(`Stopped scanning ${sym}`);
  } catch (err) { toast(err.message, true); }
});

$("#add-symbol").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("#add-symbol-input");
  const syms = input.value.split(/[\s,]+/).filter(Boolean);
  if (!syms.length) return;
  try {
    liveInfo = await api("/api/live-symbols", { body: { action: "add", symbols: syms } });
    input.value = "";
    renderChips();
    toast(`Scanning ${syms.join(", ").toUpperCase()}; the scanner picks it up within one check`);
  } catch (err) { toast(err.message, true); }
});

const LEVEL_STYLE = {
  VWAP: { color: "#fbbf24", width: 2, style: 0, label: true },
  "high of day": { color: "#94a3b8", width: 1, style: 2, label: true },
  "low of day": { color: "#94a3b8", width: 1, style: 2, label: true },
  "opening-range high": { color: "#60a5fa", width: 1, style: 1 },
  "opening-range low": { color: "#60a5fa", width: 1, style: 1 },
  "prior-day high": { color: "#a78bfa", width: 1, style: 2 },
  "prior-day low": { color: "#a78bfa", width: 1, style: 2 },
  "prior-day close": { color: "#a78bfa", width: 1, style: 1 },
};
const SHORT = {
  VWAP: "VWAP", "high of day": "HOD", "low of day": "LOD", "opening-range high": "ORH", "opening-range low": "ORL",
  "prior-day high": "PDH", "prior-day low": "PDL", "prior-day close": "PDC", "pivot R1": "R1", "pivot R2": "R2",
  "pivot S1": "S1", "pivot S2": "S2",
};

function makeCard(sym) {
  const el = document.createElement("div");
  el.className = "card";
  el.dataset.sym = sym;
  el.innerHTML = `
    <button type="button" class="expand" title="Show only ${esc(sym)}, full page" aria-label="Show only ${esc(sym)}, full page">${EXPAND_ICON}</button>
    <div class="card-head">
      <span class="sym" title="Show only ${esc(sym)}, full page">${esc(sym)}</span><span class="px num">–</span>
      <span class="stat">vs VWAP <b class="vw">–</b></span>
      <span class="stat">RSI <i class="tf-tag"></i> <b class="rsi">–</b></span>
      <span class="stat">trend <i class="tf-tag"></i> <b class="trend">–</b></span>
      <span class="stat ctx" hidden>bigger picture <b class="bias">–</b></span>
      <span class="stat qqe" hidden>QQE <i class="tf-tag"></i> <b></b></span>
      <span class="stat sr" hidden>S/R <i class="tf-tag"></i> <b></b></span>
      <span class="stat sqz" hidden>squeeze <i class="tf-tag"></i> <b></b></span>
      <span class="stat smc" hidden>smart money <i class="tf-tag"></i> <b></b></span>
      <span class="stat muted bar"></span>
    </div>
    <div class="chart"></div>
    <div class="flow" hidden>
      <div class="flow-legend"></div>
      <div class="flow-chart"></div>
    </div>
    <div class="card-foot">
      <span class="ranges">${["1H", "2H", "Today", "All"].map((r) => `<button type="button" data-range="${r}">${r}</button>`).join("")}</span>
      <span class="hold muted"></span><span class="levels"></span><span class="foot-clock"></span>
    </div>`;
  const card = { el, keys: {}, levelLines: [] };
  const box = $(".chart", el);
  card.legend = document.createElement("div");
  card.legend.className = "legend";
  box.append(card.legend);
  $(".expand", el).addEventListener("click", () => openFocus(sym));
  $(".sym", el).addEventListener("click", () => openFocus(sym));
  $(".ranges", el).addEventListener("click", (e) => e.target.dataset.range && showRange(card, e.target.dataset.range));
  el.classList.toggle("expanded", sym === focusSym);
  card.box = box;
  card.loading = document.createElement("div");
  card.loading.className = "chart-loading";
  card.loading.textContent = "Chart loads when this card is near view…";
  box.append(card.loading);
  if (chartObserver) chartObserver.observe(box);
  else loadCardChart(card);
  return card;
}

/* ---------- overlays: QQE, support / resistance breaks, squeeze releases (paperbot/qqe.py, overlays.py).
   Labels on the price chart, apart from the scanner's own markers ---------- */

class ChartLabels {
  constructor() {
    this.items = []; // { t, price, below, name, color, room }: room is the space other things already take there
    this.lines = []; // { from, to, price, color, dash, text, above, width }: lines under the candles; no `to`: to the right edge
    this.boxes = []; // { from, to, top, bottom, fill, border, text, color }: zones under the candles
    const view = (zOrder, draw) => ({ zOrder: () => zOrder, renderer: () => ({ draw }) });
    this.views = [view("bottom", (target) => this.drawLines(target)), view("top", (target) => this.draw(target))];
  }
  attached({ chart, series, requestUpdate }) { this.chart = chart; this.series = series; this.requestUpdate = requestUpdate; }
  detached() { this.chart = this.series = this.requestUpdate = null; }
  updateAllViews() {}
  paneViews() { return this.views; }
  set(items, lines, boxes) { this.items = items; this.lines = lines; this.boxes = boxes; this.requestUpdate?.(); }
  drawLines(target) {
    if (!this.chart) return;
    target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
      const ts = this.chart.timeScale();
      const x = (t, missing) => (t == null ? missing : ts.timeToCoordinate(t));
      const y = (p) => this.series.priceToCoordinate(p);
      ctx.font = "600 9px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";
      ctx.textBaseline = "middle";
      for (const b of this.boxes) {
        const x1 = x(b.from, 0), x2 = x(b.to, mediaSize.width), y1 = y(b.top), y2 = y(b.bottom);
        if (x1 == null || x2 == null || y1 == null || y2 == null || x2 < 0) continue;
        const left = Math.max(x1, 0), h = Math.max(y2 - y1, 1);
        ctx.fillStyle = b.fill;
        ctx.fillRect(left, y1, x2 - left, h);
        if (b.border) {
          ctx.strokeStyle = b.border;
          ctx.lineWidth = 1;
          ctx.strokeRect(left, y1, x2 - left, h);
        }
        if (b.text) {
          ctx.fillStyle = b.color;
          ctx.textAlign = "right";
          ctx.fillText(b.text, x2 - 6, y1 + h / 2);
        }
      }
      for (const l of this.lines) {
        const x1 = x(l.from, 0), x2 = x(l.to, mediaSize.width), y1 = y(l.price), y2 = l.price2 == null ? y1 : y(l.price2);
        if (x1 == null || x2 == null || y1 == null || y2 == null) continue;
        ctx.strokeStyle = l.color;
        ctx.lineWidth = l.width || 2;
        ctx.setLineDash(l.dash || []);
        ctx.beginPath();
        ctx.moveTo(x1, y1);
        ctx.lineTo(x2, y2);
        ctx.stroke();
        ctx.setLineDash([]);
        if (l.text) {
          ctx.fillStyle = l.color;
          ctx.textAlign = l.to == null ? "right" : "center";
          ctx.fillText(l.text, l.to == null ? x2 - 6 : (x1 + x2) / 2, (l.above ? Math.min(y1, y2) - 7 : Math.max(y1, y2) + 8));
        }
      }
    });
  }
  draw(target) {
    if (!this.chart) return;
    target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
      ctx.font = "600 10px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      const ts = this.chart.timeScale();
      const placed = []; // the labels drawn so far, so the next one moves out of their way
      for (const q of this.items) {
        const x = ts.timeToCoordinate(q.t);
        const y = this.series.priceToCoordinate(q.price);
        if (x == null || y == null || x < -60 || x > mediaSize.width + 60) continue;
        const w = ctx.measureText(q.name).width + 12, h = 16, tip = 4;
        // below the candle for a long, above it for a short, with the pointer toward the candle
        const tipY = q.below ? y + 3 + q.room : y - 3 - q.room;
        let top = q.below ? tipY + tip : tipY - tip - h;
        const clashes = () => placed.some((r) => x - w / 2 < r.x + r.w + 2 && r.x < x + w / 2 + 2 && top < r.y + h + 2 && r.y < top + h + 2);
        for (let i = 0; i < 8 && clashes(); i++) top += q.below ? h + 3 : -(h + 3);
        top = Math.max(1, Math.min(mediaSize.height - h - 1, top));
        placed.push({ x: x - w / 2, y: top, w });
        ctx.fillStyle = ctx.strokeStyle = q.color;
        ctx.lineWidth = 1;
        ctx.beginPath(); // from a label that moved, a thin line back to its candle
        ctx.moveTo(x, tipY);
        ctx.lineTo(x, q.below ? top : top + h);
        ctx.stroke();
        ctx.beginPath();
        ctx.roundRect(x - w / 2, top, w, h, 3);
        ctx.fill();
        ctx.beginPath();
        if (q.below) { ctx.moveTo(x, top - tip); ctx.lineTo(x - 5, top + 1); ctx.lineTo(x + 5, top + 1); }
        else { ctx.moveTo(x, top + h + tip); ctx.lineTo(x - 5, top + h - 1); ctx.lineTo(x + 5, top + h - 1); }
        ctx.fill();
        ctx.fillStyle = "#fff";
        ctx.fillText(q.name, x, top + h / 2 + 0.5);
      }
    });
  }
}

const EXPAND_ICON = `<svg viewBox="0 0 18 18" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" aria-hidden="true">
  <path d="M2 6V4a2 2 0 0 1 2-2h2M12 2h2a2 2 0 0 1 2 2v2M16 12v2a2 2 0 0 1-2 2h-2M6 16H4a2 2 0 0 1-2-2v-2"/></svg>`;

const LABEL_HEIGHT = 22; // a second label on the same candle goes this much further out
const GAP_COLOR = "#5b6473"; // a label that comes from the overnight gap, not from a move in the session

// one overlay's signals as [{ t, side, name, color, text }], oldest first
const OVERLAYS = {
  qqe: {
    signals: (v) => (v.qqe || []).map((q) => ({ t: q.t, side: q.side, source: "qqe", kind: "", name: q.side === "long" ? "QQE Long" : "QQE Short",
      color: q.side === "long" ? "#1f9d55" : "#e5484d",
      text: `Smoothed RSI ${num(q.rsi, 1)} crossed ${q.side === "long" ? "above" : "below"} its QQE line ${num(q.line, 1)}.` +
        (q.gap ? " It's the first candle after a big overnight gap, so the gap is part of it." : "") })),
    stat: (last) => last && `${last.side === "long" ? "Long" : "Short"} ${when(last.t)}`,
  },
  sr: {
    signals: (v) => (v.sr?.breaks || []).map((b) => {
      const up = b.side === "long";
      if (b.gap) {
        return { t: b.t, side: b.side, source: "sr", kind: b.kind, gap: true, name: up ? "S/R Gap Up" : "S/R Gap Down", color: GAP_COLOR,
          text: `Opened at ${num(b.open)}, already ${up ? "above resistance" : "below support"} ${num(b.level)}: the overnight gap broke it, not this candle.` };
      }
      return { t: b.t, side: b.side, source: "sr", kind: b.kind, name: b.kind === "wick" ? (up ? "S/R Bull Wick" : "S/R Bear Wick") : up ? "S/R Break Up" : "S/R Break Down",
        color: up ? "#0e8f8f" : "#c2680c",
        text: `Closed ${up ? "above resistance" : "below support"} ${num(b.level)}` + (b.kind === "wick"
          ? ` with a long ${up ? "lower" : "upper"} wick (volume ${b.vol >= 0 ? "+" : ""}${b.vol}%).` : ` on rising volume (+${b.vol}%).`) };
    }),
    stat: (last) => last && `${last.name.slice(4)} ${when(last.t)}`,
  },
  sqz: {
    signals: (v) => (v.sqz?.signals || []).map((x) => ({ t: x.t, side: x.side, source: "sqz", kind: "", gap: x.gap,
      name: `SQZ ${x.gap ? "Gap " : ""}${x.side === "long" ? "Long" : "Short"}`,
      color: x.gap ? GAP_COLOR : x.side === "long" ? "#6d4fd8" : "#c43a8e",
      text: `The squeeze released after ${x.candles} candle${x.candles === 1 ? "" : "s"}, with momentum ${x.mom > 0 ? "up" : "down"} (${x.mom > 0 ? "+" : ""}${x.mom}).` +
        (x.gap ? " It's the first candle after a big overnight gap, so the gap caused it." : "") })),
    stat: (last, v) => (v.sqz?.on ? `on, ${v.sqz.on} candle${v.sqz.on === 1 ? "" : "s"}` : last && `${last.name.slice(4)} ${when(last.t)}`),
  },
};

/* ---------- smart money concepts (paperbot/smc.py) ---------- */

const SMC = { up: "#14a085", down: "#e8505b" };
const smcName = (x) => `${x.internal ? "internal" : "swing"} ${x.kind} ${x.side === "long" ? "up" : "down"}${x.gap ? " (overnight gap)" : ""}`;
const biasName = (b) => (b > 0 ? "bullish" : b < 0 ? "bearish" : "–");

// what a candle's tooltip says about market structure
function smcNotes(v) {
  const m = showOverlay.smc ? v.smc : null;
  const out = [];
  if (m && showSmc.structure) {
    for (const x of m.structure) {
      const up = x.side === "long";
      out.push({ t: x.t, side: x.side, source: "smc", kind: `${x.internal ? "internal" : "swing"} ${x.kind}`, name: `SMC ${smcName(x)}`, color: x.gap ? GAP_COLOR : up ? SMC.up : SMC.down,
        text: `Closed ${up ? "above" : "below"} the ${x.internal ? "internal" : "swing"} ${up ? "high" : "low"} ${num(x.level)}: ` +
          (x.kind === "BOS" ? "the trend goes on." : "the trend may be turning.") + (x.gap ? " It opened past the level, so the overnight gap did it." : "") });
    }
  }
  if (m && showSmc.equal) {
    for (const x of m.equal) {
      out.push({ t: x.known, name: x.side === "high" ? "SMC equal highs" : "SMC equal lows", color: x.side === "high" ? SMC.down : SMC.up,
        text: `${num(x.levels[0])} and ${num(x.levels[1])}, confirmed on this candle: stops often rest ${x.side === "high" ? "above" : "below"} them.` });
    }
  }
  return out;
}

function updateSmc(card, v) {
  const m = showOverlay.smc ? v.smc : null;
  const stat = $(".stat.smc", card.el);
  stat.hidden = !m;
  if (!m) return;
  const b = $("b", stat);
  b.innerHTML = `swing <span class="${tone(m.swing)}">${biasName(m.swing)}</span>, internal <span class="${tone(m.internal)}">${biasName(m.internal)}</span>`;
  const last = m.structure.at(-1);
  stat.title = last ? `Latest: ${smcName(last)} at ${when(last.t)}, through ${num(last.level)}` : "No structure break on this chart";
}

// its lines and boxes, starting no earlier than the chart does
function smcShapes(v, bars) {
  const m = showOverlay.smc ? v.smc : null;
  if (!m || !bars.length) return { lines: [], boxes: [] };
  const first = bars[0][0];
  const from = (t) => (t < first ? null : t);
  const lines = [], boxes = [];
  const r = m.range;
  if (r && showSmc.zones) {
    const zone = (name, fill, color) => r.zones[name] && boxes.push({ from: from(r.from), top: r.zones[name][0], bottom: r.zones[name][1], fill, color,
      text: name[0].toUpperCase() + name.slice(1) });
    zone("premium", "rgba(232,80,91,.13)", "#e8808a");
    zone("equilibrium", "rgba(140,150,165,.13)", "#8a93a3");
    zone("discount", "rgba(20,160,133,.13)", "#4fc0a8");
  }
  if (showSmc.blocks) {
    for (const b of m.blocks) {
      const up = b.side === "long";
      boxes.push({ from: from(b.from), top: b.top, bottom: b.bottom, color: up ? "#7ea6f5" : "#f0a0a4",
        fill: up ? `rgba(49,121,245,${b.internal ? 0.18 : 0.28})` : `rgba(247,124,128,${b.internal ? 0.18 : 0.28})`,
        border: up ? "rgba(49,121,245,.45)" : "rgba(247,124,128,.45)" });
    }
  }
  if (showSmc.fvg) {
    const at = new Map(bars.map((b, i) => [b[0], i]));
    for (const g of m.fvg) {
      const i = at.get(g.from);
      if (i != null) boxes.push({ from: g.from, to: bars[Math.min(i + 2, bars.length - 1)][0], top: g.top, bottom: g.bottom,
        fill: g.side === "long" ? "rgba(0,230,118,.28)" : "rgba(255,60,70,.28)" });
    }
  }
  if (showSmc.structure) {
    for (const x of m.structure) {
      const up = x.side === "long";
      lines.push({ from: from(x.from), to: x.t, price: x.level, color: x.gap ? GAP_COLOR : up ? SMC.up : SMC.down, width: 1,
        dash: x.internal ? [4, 3] : null, text: x.internal ? x.kind : `${x.kind} (swing)`, above: up });
    }
  }
  if (showSmc.equal) {
    for (const x of m.equal) {
      lines.push({ from: from(x.from), to: x.t, price: x.levels[0], price2: x.levels[1], color: x.side === "high" ? SMC.down : SMC.up, width: 1,
        dash: [1, 2], text: x.side === "high" ? "EQH" : "EQL", above: x.side === "high" });
    }
  }
  if (r && showSmc.range) {
    lines.push({ from: from(r.topFrom), price: r.top, color: SMC.down, width: 1, text: m.swing < 0 ? "Strong High" : "Weak High", above: true });
    lines.push({ from: from(r.bottomFrom), price: r.bottom, color: SMC.up, width: 1, text: m.swing > 0 ? "Strong Low" : "Weak Low", above: false });
  }
  return { lines, boxes };
}

function updateOverlays(card, v, bars) {
  const byTime = new Map(bars.map((b) => [b[0], b]));
  const shown = [];
  for (const [id, o] of Object.entries(OVERLAYS)) {
    const signals = o.signals(v);
    const last = signals.at(-1);
    const text = showOverlay[id] && o.stat(last, v);
    const stat = $(`.stat.${id}`, card.el);
    stat.hidden = !text;
    if (text) {
      const b = $("b", stat);
      b.textContent = text;
      b.className = (id === "sqz" && v.sqz?.on) || last.gap ? "" : last.side === "long" ? "up" : "down";
      stat.title = id === "sqz" && v.sqz?.on ? "A quiet market: the Bollinger Bands are inside the Keltner Channels" : last.text;
    }
    if (showOverlay[id]) shown.push(...signals.filter((s) => byTime.has(s.t)));
  }
  updateSmc(card, v);
  if (!card.chart) return;
  if (!card.labels) {
    card.labels = new ChartLabels();
    card.candles.attachPrimitive(card.labels);
  }
  const levels = showOverlay.sr ? v.sr?.levels || [] : [];
  const smc = smcShapes(v, bars);
  const key = JSON.stringify([shown.map((s) => [s.t, s.name]), levels, smc]) + "|" + card.keys.bars + "|" + card.keys.markers;
  if (key === card.keys.overlays) return;
  card.keys.overlays = key;
  card.overlaysAt = new Map(); // candle time -> its overlay signals, for the tooltip
  const used = new Map(); // how far out the labels on a candle's side already reach
  const labels = shown.sort((a, b) => a.t - b.t).map((s) => {
    const below = s.side === "long";
    const at = `${s.t}|${below ? "buy" : "sell"}`;
    const room = used.get(at) ?? (card.markedAt?.has(at) ? 28 : 2);
    used.set(at, room + LABEL_HEIGHT);
    card.overlaysAt.set(s.t, [...(card.overlaysAt.get(s.t) || []), s]);
    return { t: s.t, price: byTime.get(s.t)[below ? 3 : 2], below, name: s.name, color: s.color, room };
  });
  for (const x of smcNotes(v)) card.overlaysAt.set(x.t, [...(card.overlaysAt.get(x.t) || []), x]);
  // resistance (red) and support (blue), each from the candle it was known on until the next one
  const lines = [];
  for (const [k, color] of [[1, "rgba(239,68,68,.75)"], [2, "rgba(59,130,246,.8)"]]) {
    const at = levels.filter((l) => byTime.has(l[0]));
    at.forEach((l, i) => {
      if (l[k] == null || (i && at[i - 1][k] === l[k])) return; // carried on from the previous one
      const next = at.slice(i + 1).find((m) => m[k] !== l[k]);
      const to = next ? bars[bars.findIndex((b) => b[0] === next[0]) - 1]?.[0] : bars.length ? bars[bars.length - 1][0] : null;
      if (to != null) lines.push({ from: l[0], to, price: l[k], color });
    });
  }
  card.labels.set(labels, [...smc.lines, ...lines], smc.boxes);
}

function initializeChart(card) {
  const box = $(".chart", card.el);
  const fmt = (t) => when(t);
  const tick = (t, type) => (type < 3 // a new day
    ? new Date(t * 1000).toLocaleDateString("en-US", { timeZone: ET, month: "short", day: "numeric" }) : etTime(new Date(t * 1000)));
  card.chart = LightweightCharts.createChart(box, {
    autoSize: true,
    layout: { background: { type: "solid", color: "#12161d" }, textColor: "#8a93a3", fontSize: 11 },
    grid: { vertLines: { color: "#1a2029" }, horzLines: { color: "#1a2029" } },
    rightPriceScale: { borderColor: "#232a36", minimumWidth: 84 },
    timeScale: { borderColor: "#232a36", timeVisible: true, secondsVisible: false, rightOffset: 4, tickMarkFormatter: tick },
    localization: { timeFormatter: fmt },
    crosshair: { mode: 0 },
  });
  card.candles = card.chart.addCandlestickSeries({
    upColor: "#22c55e", downColor: "#ef4444", borderVisible: false, wickUpColor: "#22c55e", wickDownColor: "#ef4444",
  });
  card.candles.priceScale().applyOptions({ scaleMargins: { top: 0.08, bottom: 0.22 } });
  card.volume = card.chart.addHistogramSeries({ priceScaleId: "", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false });
  card.volume.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
  card.chart.subscribeCrosshairMove((param) => {
    card.hover = param.point && param.time != null ? param.time : null;
    updateLegend(card);
    chartTip(card, param);
  });
  box.addEventListener("mouseleave", () => { card.hover = null; hideTip(); updateLegend(card); });
  initializeFlowChart(card);
  styleCard(card);
}

/* ---------- one chart, full page (#chart/SPY) ---------- */

const CANDLES = { // the dashboard's colours, and the full page's
  card: { up: "#22c55e", down: "#ef4444", upFaint: "34,197,94", downFaint: "239,68,68" },
  page: { up: "#089981", down: "#f23645", upFaint: "8,153,129", downFaint: "242,54,69" },
};
const palette = (card) => (card.el.classList.contains("expanded") ? CANDLES.page : CANDLES.card);

function styleCard(card) {
  if (!card.chart) return;
  const page = card.el.classList.contains("expanded");
  const c = palette(card);
  const grid = page ? { color: "rgba(255,255,255,.07)", style: 1 } : { color: "#1a2029", style: 0 };
  const base = { background: { type: "solid", color: page ? "#0f0f0f" : "#12161d" }, textColor: page ? "#c9ccd3" : "#8a93a3" };
  const border = page ? "#2e2e2e" : "#232a36";
  card.chart.applyOptions({ layout: { ...base, fontSize: page ? 12 : 11 }, grid: { vertLines: grid, horzLines: grid },
    rightPriceScale: { borderColor: border }, timeScale: { borderColor: border, rightOffset: page ? 12 : 4 } });
  card.flowChart.applyOptions({ layout: base, grid: { vertLines: grid }, rightPriceScale: { borderColor: border }, timeScale: { rightOffset: page ? 12 : 4 } });
  // the full page's live line already labels the price, with the candle's countdown
  card.candles.applyOptions({ upColor: c.up, downColor: c.down, wickUpColor: c.up, wickDownColor: c.down, lastValueVisible: !page, priceLineVisible: !page });
  card.candles.priceScale().applyOptions({ scaleMargins: page ? { top: 0.2, bottom: 0.18 } : { top: 0.08, bottom: 0.22 } });
}

function openFocus(sym) {
  location.hash = focusSym === sym ? "dashboard" : `chart/${encodeURIComponent(sym)}`; // a hash change, so Back closes it
}

function setFocus(sym) {
  if (sym === focusSym) return;
  focusSym = sym;
  hideTip();
  document.body.classList.toggle("focus", !!sym);
  document.title = sym ? `${sym} · paperbot` : "paperbot";
  if (!sym) window.scrollTo(0, 0);
  for (const [s, card] of Object.entries(cards)) {
    card.el.classList.toggle("expanded", s === sym);
    if (s === sym) loadCardChart(card);
    restyle(card);
  }
  if (lastState) {
    renderFocusBar();
    renderCards();
  }
}

// its colours and size changed: draw it again, at the zoom that suits its new width
function restyle(card) {
  if (!card.chart) return;
  styleCard(card);
  card.keys = { levels: card.keys.levels };
  card.fitted = false;
}

// the symbols to switch between, while one is shown alone
function renderFocusBar() {
  const el = $("#focus-syms");
  const syms = focusSym ? Object.keys(cards) : [];
  const key = syms.join(",") + "|" + focusSym;
  if (el.dataset.key === key) return;
  el.dataset.key = key;
  el.innerHTML = syms.map((s) => `<button type="button" data-sym="${esc(s)}" class="${s === focusSym ? "on" : ""}">${esc(s)}</button>`).join("");
}
$("#focus-syms").addEventListener("click", (e) => e.target.dataset.sym && e.target.dataset.sym !== focusSym && openFocus(e.target.dataset.sym));
$("#focus-close").addEventListener("click", () => (location.hash = "dashboard"));
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && focusSym) location.hash = "dashboard"; });

function showRange(card, range) {
  const bars = card.drawn || [];
  if (!card.chart || !bars.length) return;
  const minutes = card.tf === "1" ? 1 : lastState.bar_minutes;
  const ts = card.chart.timeScale();
  if (range === "All") return ts.fitContent();
  let from = bars.length - (range === "2H" ? 120 : 60) / minutes;
  if (range === "Today") {
    const day = etDate(bars[bars.length - 1][0]);
    const open = bars.findIndex((b) => etDate(b[0]) === day && !b[6]);
    from = open >= 0 ? open : bars.findIndex((b) => etDate(b[0]) === day);
  }
  ts.setVisibleLogicalRange({ from: Math.max(0, from), to: bars.length + (range === "Today" ? 6 : 3) });
}

// top left of a full-page chart: the candle's prices, then each indicator's latest reading
function updateLegend(card) {
  if (!card.el.classList.contains("expanded") || !card.drawn?.length) { card.legend.innerHTML = ""; return; }
  const at = card.hover != null ? card.drawn.findIndex((b) => b[0] === card.hover) : -1;
  const i = at >= 0 ? at : card.drawn.length - 1;
  const b = card.drawn[i];
  const prev = card.drawn[i - 1];
  const chg = prev ? b[4] - prev[4] : b[4] - b[1];
  const tn = b[4] >= b[1] ? "lg-up" : "lg-down";
  const val = (x) => `<span class="${tn}">${num(x)}</span>`;
  const sign = chg >= 0 ? "+" : "−";
  const pctText = ((Math.abs(chg) / (prev ? prev[4] : b[1])) * 100).toFixed(2);
  const rows = [["qqe", "QQE signals"], ["sr", "S/R Breaks"], ["sqz", "Squeeze"], ["smc", "Smart Money"]]
    .map(([id, name]) => [name, $(`.stat.${id}`, card.el)]).filter(([, el]) => !el.hidden)
    .map(([name, el]) => `<div class="lg-row">${name} <span>${$("b", el).innerHTML ? `<b class="${$("b", el).className}">${$("b", el).innerHTML}</b>` : ""}</span></div>`).join("");
  const minutes = card.tf === "1" ? 1 : lastState.bar_minutes;
  card.legend.innerHTML = `
    <div class="lg-main"><b>${esc(card.el.dataset.sym)}</b> <span class="lg-dot">·</span> ${minutes}m
      <span class="lg-ohlc">O ${val(b[1])} H ${val(b[2])} L ${val(b[3])} C ${val(b[4])} <span class="${tn}">${sign}${Math.abs(chg).toFixed(2)} (${sign}${pctText}%)</span>
      ${b[5] != null ? `Vol <span class="${tn}">${fmtVol(b[5])}</span>` : ""}</span></div>
    <div class="lg-row">${when(b[0])} <span>RSI <b>${$(".rsi", card.el).textContent}</b> · vs VWAP <b class="${$(".vw", card.el).className.replace("vw", "").trim()}">${$(".vw", card.el).textContent}</b> · trend <b class="${$(".trend", card.el).className.replace("trend", "").trim()}">${$(".trend", card.el).textContent}</b>${$(".ctx", card.el).hidden ? "" : ` · bigger picture <b class="${$(".bias", card.el).className.replace("bias", "").trim()}">${$(".bias", card.el).textContent}</b>`}</span></div>
    ${rows}`;
}

// once a second: the full page's clock, and the time left in the forming candle on its price label
setInterval(() => {
  const card = focusSym && cards[focusSym];
  if (!card) return;
  $(".foot-clock", card.el).textContent = etTime(new Date(), true) + " ET";
  if (!card.liveLine || !lastState) return;
  const secs = (card.tf === "1" ? 1 : lastState.bar_minutes) * 60;
  const left = secs - (Math.floor(Date.now() / 1000) % secs);
  const open = lastState.session && lastState.status === "running";
  card.liveLine.applyOptions({ title: open ? `${String(Math.floor(left / 60)).padStart(2, "0")}:${String(left % 60).padStart(2, "0")}` : "live" });
}, 1000);

// Order flow under the price chart: each candle's delta (green bars: more shares bought at the offer
// than sold at the bid) and the session's cumulative delta (blue line). It has a slot for every
// candle above, so the two charts line up and scroll together.
function initializeFlowChart(card) {
  const box = $(".flow-chart", card.el);
  card.flowChart = LightweightCharts.createChart(box, {
    autoSize: true,
    layout: { background: { type: "solid", color: "#12161d" }, textColor: "#8a93a3", fontSize: 10 },
    grid: { vertLines: { color: "#1a2029" }, horzLines: { visible: false } },
    rightPriceScale: { borderColor: "#232a36", minimumWidth: 84, scaleMargins: { top: 0.1, bottom: 0.1 } },
    timeScale: { visible: false, rightOffset: 4 },
    crosshair: { mode: 0, horzLine: { visible: false } },
    handleScroll: false,
    handleScale: false,
  });
  card.delta = card.flowChart.addHistogramSeries({ priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false });
  card.delta.createPriceLine({ price: 0, color: "#3b4556", lineWidth: 1, lineStyle: 2, axisLabelVisible: false });
  card.cvd = card.flowChart.addLineSeries({
    priceScaleId: "cvd", color: "#60a5fa", lineWidth: 2, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false,
  });
  card.cvd.priceScale().applyOptions({ scaleMargins: { top: 0.1, bottom: 0.1 } });
  card.chart.timeScale().subscribeVisibleLogicalRangeChange(() => syncFlow(card));
  card.chart.subscribeCrosshairMove((param) => flowLegend(card, param.time));
}

function syncFlow(card) {
  const r = card.chart?.timeScale().getVisibleLogicalRange();
  if (r && card.flowChart && card.flowShown) card.flowChart.timeScale().setVisibleLogicalRange(r);
}

function flowLegend(card, time) {
  if (!card.flowShown) return;
  const b = (time != null && card.flowAt?.get(time)) || card.flowLast;
  if (!b) return;
  $(".flow-legend", card.el).innerHTML =
    `<b>Order flow</b><span>${etTime(new Date(b[0] * 1000))} candle</span>` +
    `<span>delta <b class="${tone(b[1])}">${fmtSigned(b[1])}</b></span>` +
    `<span class="cvd-key">CVD <b class="${tone(b[2])}">${fmtSigned(b[2])}</b></span>` +
    `<span class="muted">${esc((card.flowFeed || "").toUpperCase())} trades</span>`;
}

function updateFlow(card, v, times) {
  const flow = v.flow?.bars?.length ? v.flow : null;
  const key = flow ? `${flow.bars.length}|${flow.bars[flow.bars.length - 1].join(",")}|${times.length}|${times[times.length - 1]}` : "";
  if (key === card.keys.flow) return;
  card.keys.flow = key;
  const wasShown = card.flowShown;
  card.flowShown = !!flow;
  $(".flow", card.el).hidden = !flow;
  if (!flow) {
    card.flowAt = card.flowLast = null; // so the tooltip doesn't show another timing's numbers
    return;
  }
  card.flowAt = new Map(flow.bars.map((b) => [b[0], b]));
  card.flowLast = flow.bars[flow.bars.length - 1];
  card.flowFeed = v.flow.feed;
  // a slot for every candle above (whitespace where the tape has none: pre-market, the forming candle)
  card.delta.setData(times.map((t) => {
    const b = card.flowAt.get(t);
    return b ? { time: t, value: b[1], color: b[1] >= 0 ? "rgba(34,197,94,.7)" : "rgba(239,68,68,.7)" } : { time: t };
  }));
  card.cvd.setData(times.map((t) => (card.flowAt.has(t) ? { time: t, value: card.flowAt.get(t)[2] } : { time: t })));
  flowLegend(card, null);
  syncFlow(card);
  if (!wasShown) setTimeout(() => syncFlow(card), 300); // once autoSize has sized the newly shown panel
}

function renderCards() {
  const st = lastState;
  const order = [...new Set([...(liveInfo?.symbols || []), ...Object.keys(st.symbols || {})])];
  const root = $("#cards");
  if (!order.length) {
    for (const k of Object.keys(cards)) {
      chartObserver?.unobserve(cards[k].box);
      cards[k].chart?.remove();
      cards[k].flowChart?.remove();
      delete cards[k];
    }
    root.innerHTML = `<div class="empty">Nothing to show yet. Add a symbol above; the live scanner draws it after its next check.</div>`;
    return;
  }
  $(".empty", root)?.remove();
  for (const sym of Object.keys(cards)) {
    if (!order.includes(sym)) {
      chartObserver?.unobserve(cards[sym].box);
      cards[sym].chart?.remove();
      cards[sym].flowChart?.remove();
      cards[sym].el.remove();
      delete cards[sym];
    }
  }
  order.forEach((sym, i) => {
    if (!cards[sym]) cards[sym] = makeCard(sym);
    const card = cards[sym];
    if (root.children[i] !== card.el) root.insertBefore(card.el, root.children[i] || null);
    updateCard(card, sym, st.symbols?.[sym]);
  });
  if (focusSym && !cards[focusSym]) location.hash = "dashboard"; // it isn't on the list
  else if (focusSym) {
    if (!cards[focusSym].chart) loadCardChart(cards[focusSym]);
    renderFocusBar();
  }
}

// pre-market and after-hours candles are drawn faded, as on most charts
function extColors(up, extended, pal = CANDLES.card) {
  if (!extended) return {};
  const c = `rgba(${up ? pal.upFaint : pal.downFaint},.4)`;
  return { color: c, wickColor: c, borderColor: c };
}

function updateCard(card, sym, v) {
  const el = card.el;
  const noData = (lastState.no_data || []).includes(sym);
  el.classList.toggle("nodata", noData);
  if (noData) {
    $(".bar", el).textContent = "";
    $(".chart", el).dataset.note = `Robinhood returned no data for ${sym}. Is the ticker right? Remove it with × above.`;
    return;
  }
  if (!v) {
    $(".bar", el).textContent = "waiting for the scanner's next check (within 10 seconds)";
    return;
  }
  const px = v.price;
  $(".px", el).textContent = num(px);
  const vs = px != null && v.vwap ? ((px - v.vwap) / v.vwap) * 100 : null;
  $(".vw", el).textContent = vs == null ? "–" : (vs >= 0 ? "+" : "") + vs.toFixed(2) + "%";
  $(".vw", el).className = "vw " + tone(vs);
  const cv = chartView(sym, v); // what the chart, the RSI, the trend, the overlays and the order flow show
  const tf = oneMinute() ? "1" : "base";
  const tfName = `${tf === "1" ? 1 : lastState.bar_minutes}m`;
  for (const tag of $$(".tf-tag", el)) tag.textContent = tfName;
  $(".rsi", el).textContent = num(cv.rsi, 1);
  $(".rsi", el).parentElement.title = `RSI on ${tfName} candles, as of the last one that closed`;
  $(".trend", el).textContent = cv.trend || "–";
  $(".trend", el).className = "trend " + (cv.trend === "up" ? "up" : cv.trend === "down" ? "down" : "");
  const ext = lastState.session && lastState.session !== "regular" ? ` · ${SESSION_NAMES[lastState.session]}` : "";
  if (cv.waiting) $(".bar", el).textContent = "1-minute candles load within a minute";
  else if (tf === "1") {
    const lastBar = cv.forming || cv.bars[cv.bars.length - 1];
    $(".bar", el).textContent = lastBar ? `${etTime(new Date(lastBar[0] * 1000))} 1-minute candle${cv.forming ? " forming" : ""}${ext}` : "";
  } else $(".bar", el).textContent = v.forming ? `${etTime(new Date(v.forming[0] * 1000))} candle forming${ext}`
    : v.bar_time ? `last candle ${hm(v.bar_time)}` : "";
  const ctx = v.context;
  $(".ctx", el).hidden = !ctx;
  if (ctx) {
    const b = $(".bias", el);
    b.textContent = `${ctx.bias || "–"}${ctx.score != null ? " " + (ctx.score > 0 ? "+" : "") + num(ctx.score, 1) : ""}`;
    b.className = "bias " + tone(ctx.score);
    $(".ctx", el).title = (ctx.factors || []).map((f) => `${f.side === "buy" ? "▲" : f.side === "sell" ? "▼" : "–"} ${f.text}`).join("\n");
  }

  // what you hold on this symbol
  const st = lastState;
  const shares = (st.holdings || []).filter((h) => h.symbol === sym);
  const opts = (st.options || []).filter((o) => o.symbol === sym);
  const parts = shares.map((h) => `${h.qty > 0 ? "long" : "short"} ${Math.abs(h.qty)} @ ${num(h.avg)} <b class="${tone(h.pnl)}">${money(h.pnl)}</b>`)
    .concat(opts.map((o) => `${esc(o.label)} · mark ${num(o.mark)} <b class="${tone(o.pnl)}">${money(o.pnl)} (${pct(o.pnl_pct)})</b>`));
  $(".hold", el).innerHTML = parts.length ? parts.join(" &nbsp;·&nbsp; ") : "no position";
  $(".levels", el).innerHTML = (v.levels || [])
    .map((l) => `<span class="lvl"><i>${esc(SHORT[l.name] || l.name)}</i>${num(l.price)}</span>`).join(" ");

  if (!card.chart) return;
  if (card.tf !== tf) { // switched: draw everything again
    card.tf = tf;
    card.keys = { levels: card.keys.levels };
    card.fitted = false;
  }
  const bars = [...(cv.earlier || []), ...(cv.bars || [])]; // the sessions before today's, then today's
  const barKey = bars.length ? `${bars.length}|${bars[bars.length - 1][0]}|${bars[bars.length - 1][4]}` : "";
  if (barKey !== card.keys.bars) {
    card.keys.bars = barKey;
    const pal = palette(card);
    card.candles.setData(bars.map((b) => ({ time: b[0], open: b[1], high: b[2], low: b[3], close: b[4], ...extColors(b[4] >= b[1], b[6], pal) })));
    card.volume.setData(bars.map((b) => ({ time: b[0], value: b[5], color: `rgba(${b[4] >= b[1] ? pal.upFaint : pal.downFaint},${b[6] ? 0.15 : 0.35})` })));
    if (!card.fitted && bars.length) {
      card.fitted = true;
      setTimeout(() => { // once autoSize has given the chart its width
        const ts = card.chart.timeScale();
        // the last 2 hours, or today and the afternoon before (more on a full page): scroll left for the rest
        const page = card.el.classList.contains("expanded");
        const n = (card.tf === "1" ? 120 : 150) + (page ? 60 : 0);
        if (bars.length > n + 10) ts.setVisibleLogicalRange({ from: bars.length - n, to: bars.length + (page ? 12 : 4) });
        else ts.fitContent();
      }, 300);
    }
  }
  // the candle still forming, from the scanner's live checks: it moves with every refresh
  const f = cv.forming;
  if (f && (!bars.length || f[0] > bars[bars.length - 1][0])) {
    card.candles.update({ time: f[0], open: f[1], high: f[2], low: f[3], close: f[4],
      ...extColors(f[4] >= f[1], lastState.session && lastState.session !== "regular", palette(card)) });
  }
  card.drawn = f && (!bars.length || f[0] > bars[bars.length - 1][0]) ? [...bars, [f[0], f[1], f[2], f[3], f[4], null, 0]] : bars;
  const times = bars.map((b) => b[0]);
  if (f && (!times.length || f[0] > times[times.length - 1])) times.push(f[0]);
  updateFlow(card, cv, times);
  // signal markers, snapped to the candle they belong to
  const mkKey = lastSignals.filter((s) => s.symbol === sym).map(signalId).join(",") + barKey + showOverlay.arrows;
  if (mkKey !== card.keys.markers) {
    card.keys.markers = mkKey;
    const times = bars.map((b) => b[0]);
    const snap = (t) => { let best = times[0]; for (const x of times) if (x <= t) best = x; return best; };
    const sigs = showOverlay.arrows ? lastSignals.filter((s) => s.symbol === sym && times.length) : [];
    // one marker per candle and side; only the latest few carry a label, so the chart stays readable
    const groups = new Map();
    for (const s of sigs) {
      // a signal is known when its 5-minute candle closes: on 1-minute charts, that candle's last minute
      const closes = tf === "1" && !s.live ? (lastState.bar_minutes - 1) * 60 : 0;
      const t = snap(Math.floor(new Date(s.time).getTime() / 1000) + closes);
      const k = `${t}|${s.side}`;
      const g = groups.get(k) || { time: t, side: s.side, all: [], strong: [] };
      g.all.push(s);
      if (!s.weak) g.strong.push(s);
      groups.set(k, g);
    }
    const list = [...groups.values()].sort((a, b) => a.time - b.time);
    card.markedAt = new Set(list.map((g) => `${g.time}|${g.side}`)); // so the overlay labels leave room for these markers
    card.sigsAt = new Map(); // candle time -> its signals, for the hover tooltip
    for (const g of list) card.sigsAt.set(g.time, [...(card.sigsAt.get(g.time) || []), ...g.all]);
    const labelled = new Set(list.filter((g) => g.strong.length).slice(-6));
    const markers = list.map((g) => {
      const buy = g.side === "buy";
      const lead = g.strong[0] || g.all[0];
      const own = tf === "1" && !lead.live ? `${lastState.bar_minutes}m ` : ""; // from the scanner's own candle, not this minute's
      const name = own + (lead.live ? "live" : lead.label.length > 16 ? lead.label.slice(0, 15) + "…" : lead.label);
      return {
        id: `${g.time}|${g.side}`,
        time: g.time,
        position: buy ? "belowBar" : "aboveBar",
        shape: g.strong.length ? (buy ? "arrowUp" : "arrowDown") : "circle",
        color: g.strong.length ? (buy ? "#22c55e" : "#ef4444") : "#5b6473",
        size: g.strong.length ? 1 : 0.5,
        text: labelled.has(g) ? name + (g.strong.length > 1 ? ` +${g.strong.length - 1}` : "") : "",
      };
    });
    card.candles.setMarkers(markers);
  }
  updateOverlays(card, cv, bars);
  // levels as price lines
  const lvKey = JSON.stringify(v.levels || []);
  if (lvKey !== card.keys.levels) {
    card.keys.levels = lvKey;
    card.levelLines.forEach((l) => card.candles.removePriceLine(l));
    card.levelLines = (v.levels || []).map((l) => {
      const s = LEVEL_STYLE[l.name] || { color: "#3b4556", width: 1, style: 3 };
      return card.candles.createPriceLine({
        price: l.price, color: s.color, lineWidth: s.width, lineStyle: s.style,
        axisLabelVisible: !!s.label, title: SHORT[l.name] || l.name,
      });
    });
  }
  if (px != null) {
    if (!card.liveLine) {
      card.liveLine = card.candles.createPriceLine({ price: px, color: "#e6e9ef", lineWidth: 1, lineStyle: 3, axisLabelVisible: true, title: "live" });
    } else {
      card.liveLine.applyOptions({ price: px });
    }
    const last = card.drawn[card.drawn.length - 1];
    const pal = palette(card);
    const page = card.el.classList.contains("expanded");
    card.liveLine.applyOptions(page && last ? { color: last[4] >= last[1] ? pal.up : pal.down } : { color: "#e6e9ef", title: "live" });
  }
  updateLegend(card);
}

function renderPositions() {
  const st = lastState;
  const h = st.holdings || [];
  const o = st.options || [];
  $("#pos-note").textContent = st.updated ? `as of ${hm(st.updated)}` : "";
  if (!h.length && !o.length) {
    $("#positions").innerHTML = `<div class="muted small">No positions (or the scanner hasn't run yet).</div>`;
    return;
  }
  const rows = h.map((x) => `<tr><td><b>${esc(x.symbol)}</b> <span class="muted small">${x.qty > 0 ? "long" : "short"} ${Math.abs(x.qty)}</span></td>
      <td class="num">${num(x.avg)}</td><td class="num">${num(x.price)}</td>
      <td class="num ${tone(x.pnl)}">${money(x.pnl)}</td><td class="num ${tone(x.pnl_pct)}">${pct(x.pnl_pct)}</td></tr>`)
    .concat(o.map((x) => `<tr><td><b>${esc(x.symbol)}</b> <span class="muted small">${Math.abs(x.qty)}× ${x.strike} ${x.right} ${x.expiration.slice(5)}</span></td>
      <td class="num">${num(x.avg)}</td><td class="num">${num(x.mark)}</td>
      <td class="num ${tone(x.pnl)}">${money(x.pnl)}</td><td class="num ${tone(x.pnl_pct)}">${pct(x.pnl_pct)}</td></tr>`));
  $("#positions").innerHTML = `<table class="pos"><tr><th>Position</th><th>Avg</th><th>Now</th><th>P&amp;L</th><th>%</th></tr>${rows.join("")}</table>`;
}

function feedFilter(s, sym, side, against, weak) {
  return (!sym || s.symbol === sym) && (!side || s.side === side) && (!against || s.against === true) && (weak || !s.weak);
}

function contextTag(s) {
  // from the scanner's bigger-picture layer, when it's there
  if (s.conflict) return `<span class="tag against" title="${esc(s.conflict)}">vs bigger picture</span>`;
  if (s.context && typeof s.context === "string") return `<span class="tag">${esc(s.context)}${s.context_score != null ? " " + num(s.context_score, 1) : ""}</span>`;
  return "";
}

// does order flow (delta and CVD) back this chart signal? from the scanner's check
function flowTag(s) {
  if (!s.flow) return "";
  const cls = { agrees: "with", disagrees: "against" }[s.flow] || "";
  return `<span class="tag ${cls}" title="${esc(s.flow_detail || "")}">order flow ${s.flow === "mixed" ? "mixed" : s.flow}</span>`;
}

function signalItem(s, isNew) {
  const tags = [s.live ? `<span class="tag live">live</span>` : "", s.weak ? `<span class="tag">log only</span>` : "", effectTag(s), flowTag(s), contextTag(s)].join("");
  return `<li class="${s.weak ? "weak" : ""}${isNew ? " new" : ""}">
    <div class="line1"><span class="t">${hm(s.time)}</span>${sideBadge(s.side)}<span class="sym">${esc(s.symbol)}</span>
      <span class="lab" data-explain="${esc(s.kind)}">${esc(s.label)}</span><span class="px">@ ${num(s.price)}</span>${tags}</div>
    <div class="detail">${esc(s.detail || "")}</div></li>`;
}

function renderFeed() {
  const sel = $("#feed-symbol");
  const syms = [...new Set(lastSignals.map((s) => s.symbol))].sort();
  const cur = sel.value;
  const symbolsKey = syms.join("\u001f");
  if (sel.dataset.symbols !== symbolsKey) {
    sel.dataset.symbols = symbolsKey;
    sel.innerHTML = `<option value="">All symbols</option>` + syms.map((s) => `<option${s === cur ? " selected" : ""}>${esc(s)}</option>`).join("");
  }
  const rows = lastSignals.filter((s) => feedFilter(s, sel.value, $("#feed-side").value, $("#feed-against").checked, $("#feed-weak").checked));
  const first = seen === null;
  seen = seen || new Set();
  const feedKey = `${sel.value}|${$("#feed-side").value}|${$("#feed-against").checked}|${$("#feed-weak").checked}|${rows.map(signalId).join("\u001f")}`;
  if ($("#feed").dataset.key !== feedKey) {
    $("#feed").dataset.key = feedKey;
    $("#feed").innerHTML = rows.length ? rows.slice(0, 300).map((s) => {
      const id = signalId(s);
      const isNew = !first && !seen.has(id);
      return signalItem(s, isNew);
    }).join("") : `<li class="muted">No signals yet today.</li>`;
  }
  lastSignals.forEach((s) => seen.add(signalId(s)));
  $("#feed-count").textContent = rows.length;
  const against = lastSignals.filter((s) => s.against === true && !s.weak).length;
  $("#feed-note").textContent = against ? `${against} against your positions` : "";
}
["#feed-symbol", "#feed-side", "#feed-against", "#feed-weak"].forEach((s) => $(s).addEventListener("change", renderFeed));

loaders.dashboard = refreshDashboard;
const OVERVIEW_POLL_MS = 15000;
let pollTimer = null;
let pollRunning = false;
function schedulePoll(delay) {
  clearTimeout(pollTimer);
  if (!document.hidden) pollTimer = setTimeout(poll, delay);
}
async function poll() {
  pollTimer = null;
  if (document.hidden) return;
  if (pollRunning) return schedulePoll(1000);
  pollRunning = true;
  try {
    if (activeTab() === "dashboard") await refreshDashboard();
    else await refreshPills();
  } finally {
    pollRunning = false;
    schedulePoll(activeTab() === "dashboard" ? POLL_MS : OVERVIEW_POLL_MS);
  }
}
document.addEventListener("visibilitychange", () => {
  if (document.hidden) clearTimeout(pollTimer);
  else {
    if (activeTab() === "dashboard") refreshDashboard();
    else refreshPills();
    schedulePoll(activeTab() === "dashboard" ? POLL_MS : OVERVIEW_POLL_MS);
  }
});

/* ---------- the morning report (paperbot/outlook.py) ---------- */

let reportList = [];
let report = null; // the one shown
let reportChart = null;
let reportToday = null; // today's morning report, for the alert
let reportTimer = null;

const ctTime = (iso) => new Date(iso).toLocaleTimeString("en-US", { timeZone: "America/Chicago", hour: "numeric", minute: "2-digit" }) + " CT";
const repDate = (iso) => new Date(iso).toLocaleDateString("en-US", { timeZone: ET, weekday: "short", month: "short", day: "numeric" });
const remember = (k, v) => { try { localStorage.setItem(k, v); } catch { /* private window */ } };
const recall = (k) => { try { return localStorage.getItem(k); } catch { return null; } };

// once a day: a bar across the page, a dot on the tab, and a desktop alert if they're on
function renderReportAlert(r) {
  reportToday = r || null;
  const unread = Boolean(r) && recall("reportSeen") !== r.id;
  if (unread && activeTab() === "report" && report?.id === r.id) return markReportRead();
  $("#report-dot").hidden = !unread;
  $("#report-alert").hidden = !unread;
  const title = document.title.replace(/^● /, "");
  document.title = (unread ? "● " : "") + title;
  if (!unread) return;
  $("#report-alert .ra-text").innerHTML = `<b>Today's ${esc(r.symbol)} report is ready.</b> ${esc(r.title)} · ${esc(ctTime(r.at))}`;
  if (recall("reportNotified") !== r.id && "Notification" in window && Notification.permission === "granted") {
    remember("reportNotified", r.id);
    const n = new Notification(`${r.symbol} morning report`, { body: r.title, tag: r.id });
    n.onclick = () => { window.focus(); openReport(r.id); n.close(); };
  }
}
function markReportRead() {
  if (reportToday) remember("reportSeen", reportToday.id);
  renderReportAlert(reportToday);
}
function openReport(id) {
  report = null;
  if (id) $("#rep-pick").dataset.want = id;
  if (activeTab() === "report") loaders.report();
  else showTab("report");
}
$("#report-open").addEventListener("click", () => openReport(reportToday?.id));
$("#report-dismiss").addEventListener("click", markReportRead);
setInterval(() => { // the alert, with the page in the background
  if (document.hidden) api("/api/status").then((ov) => renderReportAlert(ov.report)).catch(() => {});
}, 60000);

function renderNotifyButton() {
  $("#rep-notify").hidden = !("Notification" in window) || Notification.permission !== "default";
}
$("#rep-notify").addEventListener("click", () => Notification.requestPermission().then(renderNotifyButton));

loaders.report = async () => {
  renderNotifyButton();
  reportList = (await api("/api/reports")).reports;
  const pick = $("#rep-pick");
  const want = pick.dataset.want || pick.value || reportToday?.id || reportList[0]?.id;
  delete pick.dataset.want;
  pick.innerHTML = reportList.map((r) => `<option value="${esc(r.id)}"${r.id === want ? " selected" : ""}>${esc(repDate(r.at))}, ${esc(ctTime(r.at))} · ${esc(r.symbol)}${r.kind === "morning" ? "" : " (made on request)"}</option>`).join("");
  pick.hidden = !reportList.length;
  if (!reportList.length) {
    $("#rep-body").innerHTML = `<div class="empty">The first report comes at 10:00 AM Central on the next trading day. "Make a new one now" makes one at once.</div>`;
    return;
  }
  await loadReport(pick.value);
};
async function loadReport(id) {
  const r = await api(`/api/reports/${encodeURIComponent(id)}`);
  const same = report?.id === r.id;
  report = r;
  if (reportToday && r.id === reportToday.id) markReportRead();
  if (same) renderReportOutcome();
  else renderReport();
  clearInterval(reportTimer);
  reportTimer = setInterval(() => { // what price has done since, while the report is open
    if (activeTab() !== "report" || document.hidden || report?.day !== reportDay()) return;
    api(`/api/reports/${encodeURIComponent(report.id)}`).then((x) => {
      if (x.id !== report.id) return;
      report = x;
      renderReportOutcome();
    }).catch(() => {});
  }, 30000);
}
const reportDay = () => new Date().toLocaleDateString("en-CA", { timeZone: ET });
$("#rep-pick").addEventListener("change", () => loadReport($("#rep-pick").value));
$("#rep-now").addEventListener("click", async () => {
  const b = $("#rep-now");
  b.disabled = true;
  $("#rep-note").textContent = "Working out the levels, about 15 seconds…";
  try {
    const out = await api("/api/commands/outlook", { method: "POST" });
    $("#rep-note").textContent = out.code === 0 ? "" : "It couldn't be made: " + out.output.trim().split("\n").pop();
    if (out.code === 0) {
      $("#rep-pick").dataset.want = (await api("/api/reports")).reports[0].id;
      await loaders.report();
    }
  } catch (e) {
    $("#rep-note").textContent = e.message;
  }
  b.disabled = false;
});

const levelText = (x) => (x.low === x.high ? num(x.low) : `${num(x.low)} – ${num(x.high)}`);

function renderReport() {
  const r = report;
  const bias = { buyers: ["Buyers ahead", "up"], sellers: ["Sellers ahead", "down"], mixed: ["Neither side ahead", ""] }[r.bias];
  const rows = (list) => list.map((x) => `<li data-level="${esc(x.what)}"><span class="lv">${levelText(x)}</span><span class="what">${esc(x.what)}<i class="hit"></i></span>
    <small>${esc(x.why.join(" · "))}</small></li>`).join("") || `<li class="muted">Nothing on the chart within reach.</li>`;
  $("#rep-body").innerHTML = `
    <article class="rep-text panel">
      <h2>${esc(r.title)}</h2>
      <p class="muted">4-hour chart, levels and outlook · ${esc(repDate(r.at))}, ${esc(ctTime(r.at))} (${etTime(new Date(r.at))} ET) · price then ${num(r.price)} · <span class="${bias[1]}">${bias[0]}</span></p>
      <div id="rep-live" class="rep-live"></div>
      <h3>What's happening</h3>
      <ul class="plain">${r.facts.map((f) => `<li>${esc(f)}</li>`).join("")}</ul>
      <h3 class="bull"><i></i>Bullish scenario</h3>
      <p>${esc(r.bull.text)}</p>
      <ul class="rep-levels bull">${rows(r.bull.levels)}</ul>
      <h3 class="bear"><i></i>Bearish scenario</h3>
      <p>${esc(r.bear.text)}</p>
      <ul class="rep-levels bear">${rows(r.bear.levels)}</ul>
      <h3>The support level: ${num(r.key.price)}</h3>
      <ul class="plain">${r.support.map((f) => `<li>${esc(f)}</li>`).join("")}</ul>
      <h3>Overall outlook</h3>
      ${r.outlook.map((f) => `<p>${esc(f)}</p>`).join("")}
      <p class="muted small">Scenarios read off the chart, not a prediction and not advice. On 244 past SPY mornings the key level bounced 54% of the times price touched it;
        a level picked at a random distance, 50%. Dates and hours in the text are New York time.</p>
    </article>
    <div class="rep-chart panel">
      <div class="rep-legend"><b>${esc(r.symbol)}</b> 4h · 180 days, around the clock
        <span style="color:#f1f1e6">20 EMA</span><span style="color:#f23645">50 EMA</span><span style="color:#3aa655">200 SMA</span>
        <span style="color:#a64bd6">falling line</span><span style="color:#4aa8ff">rising line</span></div>
      <div class="rep-canvas"><div class="chart-loading">Loading the chart…</div></div>
    </div>`;
  renderReportOutcome();
  drawReportChart();
}

// whether the support has held, and the targets reached
function renderReportOutcome() {
  const o = report?.outcome, el = $("#rep-live");
  if (!o || !el) return;
  const key = num(report.key.price), at = (iso) => (iso ? etTime(new Date(iso)) + " ET" : "");
  const line = {
    waiting: ["", `Support ${key}: no candles since the report yet.`],
    holding: ["ok", `✓ Support ${key} has held since the report. Lowest since: ${num(o.low)}.`],
    lost: ["bad", `✗ Support ${key} was lost: a half-hour candle closed below it at ${at(o.lost_at)}. Lowest since: ${num(o.low)}.`],
    reclaimed: ["warn", `Support ${key} was lost at ${at(o.lost_at)}, then won back at ${at(o.reclaimed_at)}. Lowest since: ${num(o.low)}.`],
  }[o.status];
  el.className = "rep-live " + line[0];
  el.textContent = line[1] + (o.last != null ? ` Highest ${num(o.high)}, last ${num(o.last)}.` : "");
  $$("#rep-body [data-level]").forEach((li) => {
    const hit = o.hit.find((h) => h.what === li.dataset.level);
    $(".hit", li).textContent = hit ? ` ✓ reached ${at(hit.at)}` : "";
  });
}

function drawReportChart() {
  const r = report, box = $(".rep-canvas");
  loadChartLibrary().then(() => {
    if (report?.id !== r.id || !box.isConnected) return;
    reportChart?.remove();
    box.innerHTML = "";
    const bars = r.chart.bars;
    const tick = (t, kind) => (kind <= 2 ? new Date(t * 1000).toLocaleDateString("en-US", { timeZone: ET, month: "short", day: "numeric" }) : etTime(new Date(t * 1000)));
    const chart = (reportChart = LightweightCharts.createChart(box, {
      autoSize: true, layout: { background: { type: "solid", color: "#000" }, textColor: "#b8bcc6", fontSize: 11 },
      grid: { vertLines: { visible: false }, horzLines: { color: "#26282d" } }, rightPriceScale: { borderColor: "#2e2e2e", minimumWidth: 84 },
      timeScale: { borderColor: "#2e2e2e", timeVisible: true, secondsVisible: false, rightOffset: 10, tickMarkFormatter: tick },
      localization: { timeFormatter: (t) => `${repDate(t * 1000)}, ${etTime(new Date(t * 1000))} ET` }, crosshair: { mode: 0 } }));
    const candles = chart.addCandlestickSeries({ upColor: "#22a94a", downColor: "#f23645", borderVisible: false, wickUpColor: "#22a94a", wickDownColor: "#f23645",
      priceLineStyle: 2, autoscaleInfoProvider: (original) => { // every level in view, not only the candles
        const a = original();
        if (a) {
          a.priceRange.minValue = Math.min(a.priceRange.minValue, r.key.price, ...r.bear.levels.map((x) => x.low));
          a.priceRange.maxValue = Math.max(a.priceRange.maxValue, r.key.price, ...r.bull.levels.map((x) => x.high));
        }
        return a;
      } });
    candles.priceScale().applyOptions({ scaleMargins: { top: 0.06, bottom: 0.14 } });
    candles.setData(bars.map((b) => ({ time: b[0], open: b[1], high: b[2], low: b[3], close: b[4] })));
    const volume = chart.addHistogramSeries({ priceScaleId: "", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false, color: "rgba(150,155,165,.35)" });
    volume.priceScale().applyOptions({ scaleMargins: { top: 0.86, bottom: 0 } });
    volume.setData(bars.map((b) => ({ time: b[0], value: b[5] })));
    const plain = { lineWidth: 2, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false, autoscaleInfoProvider: () => null };
    for (const [k, color] of [["ema20", "#f1f1e6"], ["ema50", "#f23645"], ["sma200", "#3aa655"]]) {
      chart.addLineSeries({ ...plain, lineWidth: 1, color }).setData(r.chart[k].map(([time, value]) => ({ time, value })));
    }
    for (const l of r.chart.lines) {
      chart.addLineSeries({ ...plain, color: l.falling ? "#a64bd6" : "#4aa8ff" }).setData([{ time: l.from, value: l.a }, { time: l.to, value: l.b }]);
    }
    candles.createPriceLine({ price: r.key.price, color: "#ffffff", lineWidth: 2, lineStyle: 0, axisLabelVisible: true, title: "support to hold" });
    const boxes = [];
    for (const [list, color, rgb] of [[r.bull.levels, "#4fc07a", "58,166,85"], [r.bear.levels, "#f07b84", "242,54,69"]]) {
      for (const x of list) {
        if (x.low === x.high) candles.createPriceLine({ price: x.low, color, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: x.what });
        else boxes.push({ from: null, top: x.high, bottom: x.low, fill: `rgba(${rgb},.16)`, border: `rgba(${rgb},.35)`, color, text: `${x.what}  ${levelText(x)}` });
      }
    }
    const marks = new ChartLabels();
    candles.attachPrimitive(marks);
    marks.set([], [], boxes);
    chart.timeScale().setVisibleLogicalRange({ from: bars.length - 92, to: bars.length + 10 });
  }).catch(() => { box.innerHTML = `<div class="chart-loading error">Chart unavailable. Check your internet connection.</div>`; });
}

/* ---------- signals history ---------- */

let histRows = [];
let kindFilter = "";

async function loadDates(select, keep) {
  const d = await api("/api/dates");
  const days = d.dates.includes(d.today) ? d.dates : [d.today, ...d.dates];
  const cur = keep || select.value || d.today;
  select.innerHTML = days.map((x) => `<option${x === cur ? " selected" : ""}>${x}</option>`).join("");
}

loaders.signals = async () => {
  await loadDates($("#sig-date"));
  await loadHistory();
};

async function loadHistory() {
  const d = $("#sig-date").value;
  const r = await api(`/api/signals?date=${encodeURIComponent(d)}`);
  histRows = r.signals;
  const syms = [...new Set(histRows.map((s) => s.symbol))].sort();
  const cur = $("#sig-symbol").value;
  $("#sig-symbol").innerHTML = `<option value="">All symbols</option>` + syms.map((s) => `<option${s === cur ? " selected" : ""}>${esc(s)}</option>`).join("");
  renderHistory();
}

function renderHistory() {
  const sym = $("#sig-symbol").value, side = $("#sig-side").value, q = $("#sig-search").value.toLowerCase();
  let rows = histRows.filter((s) => feedFilter(s, sym, side, $("#sig-against").checked, $("#sig-weak").checked));
  if (q) rows = rows.filter((s) => (s.label + " " + (s.detail || "") + " " + s.symbol).toLowerCase().includes(q));
  const counts = {};
  rows.forEach((s) => (counts[s.label] = (counts[s.label] || 0) + 1));
  $("#sig-kinds").innerHTML = Object.entries(counts).sort((a, b) => b[1] - a[1])
    .map(([k, n]) => `<span class="tag${k === kindFilter ? " on" : ""}" data-kind="${esc(k)}">${esc(k)} ${n}</span>`).join("") || `<span class="muted small">No signals.</span>`;
  if (kindFilter) rows = rows.filter((s) => s.label === kindFilter);
  $("#sig-note").textContent = `${rows.length} signals`;
  $("#sig-table").innerHTML = `<tr><th>Time</th><th>Symbol</th><th>Side</th><th>Signal</th><th>Price</th><th>What happened</th><th>Your position</th></tr>` +
    rows.map((s) => `<tr class="${s.weak ? "muted" : ""}"><td class="mono">${hm(s.time)}</td><td><b>${esc(s.symbol)}</b></td><td>${sideBadge(s.side)}</td>
      <td><span data-explain="${esc(s.kind)}">${esc(s.label)}</span>${s.live ? ` <span class="tag live">live</span>` : ""}${s.weak ? ` <span class="tag">log only</span>` : ""}</td>
      <td class="mono">${num(s.price)}</td><td class="small">${esc(s.detail || "")}</td><td>${effectTag(s)} ${flowTag(s)} ${contextTag(s)}</td></tr>`).join("");
}
$("#sig-date").addEventListener("change", loadHistory);
["#sig-symbol", "#sig-side", "#sig-against", "#sig-weak"].forEach((s) => $(s).addEventListener("change", renderHistory));
$("#sig-search").addEventListener("input", renderHistory);
$("#sig-kinds").addEventListener("click", (e) => {
  const k = e.target.dataset.kind;
  if (k === undefined) return;
  kindFilter = kindFilter === k ? "" : k;
  renderHistory();
});

/* ---------- signal audit (paperbot/signal_audit.py) ---------- */

const AUDIT_BAD = ["wrong", "missed", "vanished"];
const AUDIT_NAMES = { ok: "Right", wrong: "Wrong", missed: "Missed", vanished: "Disappeared", wording: "Wording", changed: "Candle corrected",
  waiting: "Checking", unknown: "No check" };
const AUDIT_SOURCES = { scan: "Scanner", live: "Live cross", qqe: "QQE", sr: "S/R", sqz: "Squeeze", smc: "Smart Money" };
let auditData = null; // the day on the Audit tab
let auditToday = null; // today's, for the charts' tooltips
let auditByKey = new Map();
let auditStamp = null;

const signedPct = (x) => (x == null ? "–" : `${x > 0 ? "+" : x < 0 ? "−" : ""}${Math.abs(x).toFixed(2)}%`);
const auditBad = (c) => AUDIT_BAD.reduce((n, k) => n + (c?.[k] || 0), 0);

function renderAuditPill(a) {
  const el = $("#pill-audit");
  const bad = auditBad(a?.counts);
  el.textContent = !a || a.status === "off" ? "Audit off" : a.status === "stale" ? "Audit stale"
    : `Audit: ${a.total} checked${bad ? ` · ${[["wrong", (a.counts.wrong || 0) + (a.counts.vanished || 0)], ["missed", a.counts.missed || 0]].filter((x) => x[1]).map((x) => `${x[1]} ${x[0]}`).join(", ")}` : ""}`;
  el.className = "pill " + (!a || a.status === "off" || a.status === "stale" ? "warn" : bad ? "bad" : "ok");
  if (a?.updated && a.updated !== auditStamp) { // new results: for the tooltips, and the tab if it's open
    auditStamp = a.updated;
    api("/api/audit").then((d) => {
      auditToday = d;
      auditByKey = new Map(d.records.map((r) => [r.key, r]));
      if (activeTab() === "audit" && (!auditData || auditData.day === d.day)) { auditData = d; renderAudit(); }
    }).catch(() => {});
  }
}
$("#pill-audit").addEventListener("click", () => showTab("audit"));
$("#pill-audit").addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") showTab("audit"); });

// the audit's line in a tooltip
function auditHtml(key) {
  const r = auditByKey.get(key);
  if (!r) return "";
  const parts = [];
  if (r.verdict === "ok") parts.push("✓ checked: right");
  else if (r.verdict === "waiting") parts.push("being checked");
  else parts.push(`${r.verdict === "wording" ? "!" : r.verdict === "unknown" ? "" : "✗"} ${AUDIT_NAMES[r.verdict].toLowerCase()}${r.faults.length ? `: ${r.faults.join("; ")}` : ""}`);
  if (r.second === "agrees") parts.push("a second source agrees");
  if (r.second === "differs") parts.push("a close call: not there on the full-market candles");
  if (r.after?.["30"] != null) parts.push(`30 min later price had gone ${signedPct(r.after["30"])} its way`);
  return `<div class="tip-detail audit-${esc(r.verdict)}">Audit ${esc(parts.join(" · "))}</div>`;
}

loaders.audit = async () => {
  await loadAudit();
};
async function loadAudit() {
  const d = $("#aud-date").value;
  auditData = await api(`/api/audit${d ? `?date=${encodeURIComponent(d)}` : ""}`);
  const days = auditData.days.includes(auditData.day) ? auditData.days : [auditData.day, ...auditData.days];
  $("#aud-date").innerHTML = days.map((x) => `<option${x === auditData.day ? " selected" : ""}>${esc(x)}</option>`).join("");
  renderAudit();
}

function renderAudit() {
  const a = auditData, c = a.counts || {};
  const keep = (sel, html) => { const el = $(sel), cur = el.value; el.innerHTML = html; el.value = cur; };
  keep("#aud-symbol", `<option value="">All symbols</option>` + [...new Set(a.records.map((r) => r.symbol))].sort().map((s) => `<option>${esc(s)}</option>`).join(""));
  keep("#aud-source", `<option value="">Every signal</option>` + a.table.map((g) =>
    `<option value="${esc(g.source)}|${g.tf}">${esc(g.name)}${g.tf ? ` ${g.tf}m` : ""}</option>`).join(""));
  const asked = (c.second_agrees || 0) + (c.second_differs || 0);
  const done = a.table.reduce((n, g) => n + g.done, 0), right = a.table.reduce((n, g) => n + g.right, 0);
  const usual = a.table.reduce((n, g) => n + (g.usual_right ?? 0) * g.done, 0);
  const off = Object.values(a.candles || {}).reduce((n, x) => n + x.differ.length, 0);
  const compared = Object.values(a.candles || {}).reduce((n, x) => n + x.compared, 0);
  const tile = (name, value, note, cls = "") => `<div class="aud-tile"><span>${esc(name)}</span><b class="${cls}">${esc(value)}</b><small>${esc(note)}</small></div>`;
  $("#aud-tiles").innerHTML =
    tile("Signals checked", a.total || 0, a.updated ? `last look ${etTime(new Date(a.updated), true)} ET` : "the audit hasn't run") +
    tile("Right", c.ok || 0, "worked out again, same answer", "up") +
    tile("Wrong", (c.wrong || 0) + (c.vanished || 0), "says something its candles don't", (c.wrong || c.vanished) ? "down" : "") +
    tile("Missed", c.missed || 0, "should have fired and didn't", c.missed ? "down" : "") +
    tile("Wording", c.wording || 0, "right, but its text was loose") +
    tile("Second source", asked ? `${Math.round((100 * (c.second_agrees || 0)) / asked)}%` : "–", `${c.second_agrees || 0} of ${asked} also there on full-market candles`) +
    tile("Candles", compared ? `${compared - off} of ${compared}` : "–", "match the full market's") +
    tile("Went its way", done ? `${Math.round((100 * right) / done)}%` : "–", done ? `after 30 min; any candle: ${Math.round((100 * usual) / done)}%` : "after 30 min");
  $("#aud-groups").innerHTML = `<tr><th>Indicator</th><th>Signals</th><th>Right</th><th>Wrong or missed</th><th>Close calls</th><th>Went its way</th><th>Any candle</th><th>Average move</th><th>Any candle</th></tr>` +
    a.table.map((g) => `<tr><td><b>${esc(g.name)}</b>${g.tf ? ` ${g.tf}m` : ""}</td><td class="mono">${g.n}</td><td class="mono">${g.ok}</td>
      <td class="mono ${g.bad ? "down" : ""}">${g.bad}</td><td class="mono">${g.differs}</td>
      <td class="mono">${g.done ? `${Math.round((100 * g.right) / g.done)}% of ${g.done}` : "–"}</td><td class="mono muted">${g.usual_right == null ? "–" : Math.round(100 * g.usual_right) + "%"}</td>
      <td class="mono ${tone(g.avg)}">${signedPct(g.avg)}</td><td class="mono muted">${signedPct(g.usual_avg)}</td></tr>`).join("");
  const sym = $("#aud-symbol").value, src = $("#aud-source").value, v = $("#aud-verdict").value;
  const rows = a.records.filter((r) => (!sym || r.symbol === sym) && (!src || `${r.source}|${r.tf}` === src) &&
    (!v || (v === "problems" ? AUDIT_BAD.includes(r.verdict) : v === "differs" ? r.second === "differs" : r.verdict === v)));
  const status = { running: "checking once a minute", idle: "waiting for the next session", stale: "not running", off: "not running", past: "" }[a.status];
  $("#aud-note").textContent = `${rows.length} shown${status ? ` · ${status}` : ""}`;
  const second = { agrees: "agrees", differs: "close call", none: "–" };
  const result = { right: `<span class="up">went its way</span>`, wrong: `<span class="down">went against</span>`, flat: "flat" };
  $("#aud-table").innerHTML = `<tr><th>Time</th><th>Symbol</th><th>Chart</th><th>Side</th><th>Signal</th><th>Check</th><th>Why</th><th>Second source</th><th>15 min</th><th>30 min</th><th>After 30 min</th></tr>` +
    (rows.slice(0, 600).map((r) => `<tr><td class="mono">${hm(r.time)}</td><td><b>${esc(r.symbol)}</b></td><td>${r.tf ? r.tf + "m" : "live"}</td><td>${sideBadge(r.side)}</td>
      <td title="${esc(r.detail || "")}">${esc(r.name)}</td><td><span class="tag audit-tag ${esc(r.verdict)}">${esc(AUDIT_NAMES[r.verdict] || r.verdict)}</span></td>
      <td class="small">${esc([...r.faults, ...(r.second_faults || [])].join("; "))}${(r.late || 0) > 120 ? ` <span class="muted">logged ${Math.round(r.late / 60)} min late</span>` : ""}</td>
      <td class="small">${second[r.second] || "16 min after"}</td><td class="mono ${tone(r.after?.["15"])}">${signedPct(r.after?.["15"])}</td>
      <td class="mono ${tone(r.after?.["30"])}">${signedPct(r.after?.["30"])}</td><td class="small">${result[r.result] || "–"}</td></tr>`).join("") ||
      `<tr><td colspan="11" class="muted">${v === "problems" ? "No problems found." : "Nothing here."}</td></tr>`) +
    (rows.length > 600 ? `<tr><td colspan="11" class="muted">The first 600 of ${rows.length}.</td></tr>` : "");
}
$("#aud-date").addEventListener("change", loadAudit);
["#aud-symbol", "#aud-source", "#aud-verdict"].forEach((s) => $(s).addEventListener("change", renderAudit));

/* ---------- patterns ---------- */

let patData = null;
const GROUP_NAMES = { classic: "Classic", candlestick: "Candlestick", fibonacci: "Fibonacci" };

loaders.patterns = async () => {
  patData = await api("/api/patterns");
  renderPatterns();
};

function renderPatterns() {
  $("#pat-groups").innerHTML = patData.groups.map((g) =>
    `<span class="chip toggle${patData.on.includes(g) ? " on" : ""}" data-group="${g}">${GROUP_NAMES[g] || g}</span>`).join("");
  const q = $("#pat-search").value.toLowerCase();
  let html = "";
  for (const g of patData.groups) {
    const rows = patData.patterns.filter((p) => p.group === g && (!q || [p.label, p.kind, p.about, p.meaning].join(" ").toLowerCase().includes(q)));
    if (!rows.length) continue;
    const on = patData.on.includes(g);
    html += `<div class="pat-group-title">${GROUP_NAMES[g] || g} · ${rows.length}${on ? "" : " · group off"}</div>`;
    html += rows.map((p) => `<div class="pat${on ? "" : " groupoff"}">
        <div class="name"><b data-explain="${esc(p.kind)}">${esc(p.label)}</b><span>${esc(p.about || p.meaning || p.kind)}</span></div>
        <div class="seg" data-kind="${p.kind}">${["alert", "log", "off"].map((v) =>
          `<button data-v="${v}" class="${(on ? p.state : "off") === v ? "on" : ""}"${on ? "" : " disabled"}>${v === "alert" ? "Alert" : v === "log" ? "Log" : "Off"}</button>`).join("")}</div>
      </div>`).join("");
  }
  $("#pat-list").innerHTML = html || `<div class="empty">No pattern matches.</div>`;
}

$("#pat-search").addEventListener("input", renderPatterns);
$("#pat-groups").addEventListener("click", async (e) => {
  const g = e.target.dataset.group;
  if (!g) return;
  const on = patData.on.includes(g) ? patData.on.filter((x) => x !== g) : [...patData.on, g];
  try {
    patData = await api("/api/patterns", { body: { groups: on } });
    renderPatterns();
    toast(`${GROUP_NAMES[g]} patterns ${on.includes(g) ? "on" : "off"}`);
  } catch (err) { toast(err.message, true); }
});
$("#pat-list").addEventListener("click", async (e) => {
  const v = e.target.dataset.v;
  const kind = e.target.closest(".seg")?.dataset.kind;
  if (!v || !kind) return;
  try {
    patData = await api("/api/patterns", { body: { set: { [kind]: v } } });
    renderPatterns();
  } catch (err) { toast(err.message, true); }
});

/* ---------- settings ---------- */

const MANAGED_ELSEWHERE = new Set(["pattern_groups", "disabled_patterns", "log_only_patterns", "oauth_callback_port"]);
let setData = null;

loaders.settings = async () => {
  const [s, c] = await Promise.all([api("/api/settings"), api("/api/config")]);
  setData = s;
  renderSettings();
  $("#config-json").textContent = `# ${c.path}\n` + JSON.stringify(c.config, null, 2);
};

function fieldInput(key, spec, value) {
  const enumVals = spec.enum || (spec.allOf && spec.allOf[0]?.enum);
  if (enumVals) return `<select name="${key}">${enumVals.map((x) => `<option${x === value ? " selected" : ""}>${x}</option>`).join("")}</select>`;
  if (spec.type === "boolean") return `<input type="checkbox" name="${key}"${value ? " checked" : ""}>`;
  if (spec.type === "integer" || spec.type === "number") {
    const min = spec.minimum ?? spec.exclusiveMinimum, max = spec.maximum ?? spec.exclusiveMaximum;
    return `<input type="number" name="${key}" value="${value}" step="${spec.type === "integer" ? 1 : "any"}"${min != null ? ` min="${min}"` : ""}${max != null ? ` max="${max}"` : ""}>`;
  }
  if (spec.type === "array" && spec.items?.enum) {
    return `<span class="checks">${spec.items.enum.map((x) => `<label><input type="checkbox" name="${key}" value="${x}"${(value || []).includes(x) ? " checked" : ""}> ${x}</label>`).join(" ")}</span>`;
  }
  if (spec.type === "array") return `<input type="text" class="wide" name="${key}" value="${esc((value || []).join(", "))}" placeholder="comma-separated">`;
  return `<input type="text" name="${key}" value="${esc(value)}">`;
}

function show(v) { return Array.isArray(v) ? (v.length ? v.join(", ") : "none") : String(v); }

function renderSettings() {
  const s = setData;
  $("#set-note").textContent = Object.keys(s.changes).length
    ? `${Object.keys(s.changes).length} changed from config.yaml${s.updated ? `, last by ${s.by} at ${hm(s.updated)}` : ""}. A running scan applies changes within one check.`
    : "Same as config.yaml. Changes here apply to a running scan within one check.";
  $("#set-form").innerHTML = Object.entries(s.fields).filter(([k]) => !MANAGED_ELSEWHERE.has(k)).map(([k, spec]) => {
    const changed = k in s.changes;
    return `<div class="field${changed ? " changed" : ""}" data-key="${k}">
      <span class="name">${k}</span>${fieldInput(k, spec, s.effective[k])}
      <span class="help">${esc(spec.description || "")}</span>
      <span class="base">config.yaml: ${esc(show(s.base[k]))}${changed ? ` · <a href="#" data-reset="${k}">reset</a>` : ""}</span>
    </div>`;
  }).join("");
}

function readField(key) {
  const spec = setData.fields[key];
  const el = $(`#set-form [name="${key}"]`);
  if (spec.type === "boolean") return el.checked;
  if (spec.type === "array" && spec.items?.enum) return $$(`#set-form [name="${key}"]`).filter((x) => x.checked).map((x) => x.value);
  if (spec.type === "array") return el.value.split(/[\s,]+/).filter(Boolean).map((x) => x.toUpperCase());
  if (spec.type === "integer" || (spec.enum || spec.allOf) && typeof setData.effective[key] === "number") return parseInt(el.value, 10);
  if (spec.type === "number") return parseFloat(el.value);
  return el.value;
}

function pendingChanges() {
  const out = {};
  for (const k of Object.keys(setData.fields)) {
    if (MANAGED_ELSEWHERE.has(k) || !$(`#set-form [name="${k}"]`)) continue;
    const v = readField(k);
    if (JSON.stringify(v) !== JSON.stringify(setData.effective[k])) out[k] = v;
  }
  return out;
}

$("#set-form").addEventListener("input", (e) => {
  const f = e.target.closest(".field");
  if (f) f.classList.toggle("dirty", f.dataset.key in pendingChanges());
});
$("#set-form").addEventListener("click", async (e) => {
  const k = e.target.dataset.reset;
  if (!k) return;
  e.preventDefault();
  try { setData = await api("/api/settings", { body: { reset: [k] } }); renderSettings(); toast(`${k} back to config.yaml`); }
  catch (err) { toast(err.message, true); }
});
$("#set-save").addEventListener("click", async () => {
  const changes = pendingChanges();
  if (!Object.keys(changes).length) return toast("Nothing changed");
  try { setData = await api("/api/settings", { body: { changes } }); renderSettings(); toast(`Saved ${Object.keys(changes).length} setting(s)`); }
  catch (err) { toast(err.message, true); }
});
$("#set-reset").addEventListener("click", async () => {
  if (!confirm("Put every scanner setting back to config.yaml?")) return;
  try { setData = await api("/api/settings", { body: { reset: true } }); renderSettings(); toast("Back to config.yaml"); }
  catch (err) { toast(err.message, true); }
});

/* ---------- paper bot ---------- */

loaders.bot = async () => {
  const [h, cmds] = await Promise.all([api("/api/killswitch"), api("/api/commands")]);
  renderHalts(h);
  $("#commands").innerHTML = cmds.map((c) =>
    `<button class="cmd${c.changes ? " changes" : ""}" data-cmd="${c.name}" data-changes="${c.changes}" data-date="${c.date}">
      <b>${esc(c.label)}</b><span>${esc(c.about)}</span></button>`).join("");
  if (!$("#cmd-date").value) $("#cmd-date").value = new Date().toLocaleDateString("en-CA", { timeZone: ET });
  await loadDates($("#jr-date"));
  await loadJournal();
};

function renderHalts(h) {
  const row = (name, v) => `<div class="halt"><b>${name}</b><span class="${v ? "down" : "up"}">${v ? esc(v) : "clear"}</span></div>`;
  $("#halts").innerHTML = row("AI entries (HALT)", h.manual) + row("Everything (HALT_ALL)", h.all) + row("Today's loss halt", h.day);
}

$$("[data-halt]").forEach((b) => b.addEventListener("click", async () => {
  const action = b.dataset.halt;
  const ask = { halt: "Stop the AI from opening trades? Exits keep running.", halt_all: "Stop everything, including exit management?", resume: "Clear HALT and HALT_ALL?" }[action];
  if (!confirm(ask)) return;
  try { renderHalts(await api("/api/killswitch", { body: { action } })); toast("Done"); refreshPills(); }
  catch (err) { toast(err.message, true); }
}));

$("#commands").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-cmd]");
  if (!b) return;
  const name = b.dataset.cmd;
  if (b.dataset.changes === "true" && !confirm(`Run "${b.querySelector("b").textContent}"? ${b.querySelector("span").textContent}.`)) return;
  const day = b.dataset.date === "true" ? $("#cmd-date").value : "";
  $$("#commands button").forEach((x) => (x.disabled = true));
  $("#cmd-output").textContent = `Running ${name}…`;
  $("#out-note").textContent = "";
  try {
    const r = await api(`/api/commands/${name}${day ? `?date=${day}` : ""}`, { method: "POST" });
    $("#cmd-output").textContent = `$ ${r.command}\n\n${r.output || "(no output)"}`;
    $("#out-note").textContent = `exit ${r.code} · ${r.seconds} s`;
  } catch (err) {
    $("#cmd-output").textContent = err.message;
  } finally {
    $$("#commands button").forEach((x) => (x.disabled = false));
  }
});

async function loadJournal() {
  const d = $("#jr-date").value;
  const ev = $("#jr-event").value;
  const r = await api(`/api/journal?date=${encodeURIComponent(d)}${ev ? `&event=${encodeURIComponent(ev)}` : ""}`);
  $("#jr-event").innerHTML = `<option value="">Every event (${Object.values(r.counts).reduce((a, b) => a + b, 0)})</option>` +
    Object.entries(r.counts).sort().map(([k, n]) => `<option value="${esc(k)}"${k === ev ? " selected" : ""}>${esc(k)} (${n})</option>`).join("");
  $("#jr-table").innerHTML = `<tr><th>Time</th><th>Event</th><th>Details</th></tr>` + r.events.slice(0, 1000).map((e) => {
    const rest = Object.entries(e).filter(([k]) => k !== "ts" && k !== "event")
      .map(([k, v]) => `<b>${esc(k)}</b>=${esc(typeof v === "object" ? JSON.stringify(v) : v)}`).join("  ");
    return `<tr><td class="mono">${e.ts ? etTime(new Date(e.ts), true) : ""}</td><td>${esc(e.event)}</td><td class="small">${rest}</td></tr>`;
  }).join("");
}
$("#jr-date").addEventListener("change", loadJournal);
$("#jr-event").addEventListener("change", loadJournal);

function refreshPills() { return api("/api/status").then(renderPills).catch(() => {}); }

/* ---------- start ---------- */

const initialTab = (location.hash || "#dashboard").slice(1);
if (initialTab !== "dashboard") refreshPills();
showTab(initialTab);
