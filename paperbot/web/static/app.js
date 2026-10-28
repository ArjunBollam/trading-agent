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

function effectTag(s) {
  if (s.against === true) return `<span class="tag against">against your position</span>`;
  if (s.against === false && s.held) return `<span class="tag with">with your position</span>`;
  return "";
}

/* ---------- tabs ---------- */

const loaders = {};
function showTab(name) {
  if (!$(`#tab-${name}`)) name = "dashboard";
  $$("#nav button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach((t) => t.classList.toggle("active", t.id === `tab-${name}`));
  if (location.hash !== `#${name}`) history.replaceState(null, "", `#${name}`);
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
    try {
      const { overview: ov, state: st, signals: sg } = await api("/api/dashboard");
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
  const st = lastState;
  $("#live-note").textContent =
    st.status === "off" ? "The live scanner hasn't written anything yet." :
    `Candles every ${st.bar_minutes} min, live price every ${st.tick_seconds || "–"} s · updated ${hm(st.updated)}` +
    (liveInfo.source === "file" && liveInfo.by ? ` · list changed by ${liveInfo.by}` : "");
}

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
    <div class="card-head">
      <span class="sym">${esc(sym)}</span><span class="px num">–</span>
      <span class="stat">vs VWAP <b class="vw">–</b></span>
      <span class="stat">RSI <b class="rsi">–</b></span>
      <span class="stat">trend <b class="trend">–</b></span>
      <span class="stat ctx" hidden>bigger picture <b class="bias">–</b></span>
      <span class="stat muted bar"></span>
    </div>
    <div class="chart"></div>
    <div class="card-foot"><span class="hold muted"></span><span class="levels"></span></div>`;
  const card = { el, keys: {}, levelLines: [] };
  const box = $(".chart", el);
  card.box = box;
  card.loading = document.createElement("div");
  card.loading.className = "chart-loading";
  card.loading.textContent = "Chart loads when this card is near view…";
  box.append(card.loading);
  if (chartObserver) chartObserver.observe(box);
  else loadCardChart(card);
  return card;
}

function initializeChart(card) {
  const box = $(".chart", card.el);
  const fmt = (t) => etTime(new Date(t * 1000));
  card.chart = LightweightCharts.createChart(box, {
    autoSize: true,
    layout: { background: { type: "solid", color: "#12161d" }, textColor: "#8a93a3", fontSize: 11 },
    grid: { vertLines: { color: "#1a2029" }, horzLines: { color: "#1a2029" } },
    rightPriceScale: { borderColor: "#232a36" },
    timeScale: { borderColor: "#232a36", timeVisible: true, secondsVisible: false, rightOffset: 4, tickMarkFormatter: fmt },
    localization: { timeFormatter: fmt },
    crosshair: { mode: 0 },
  });
  card.candles = card.chart.addCandlestickSeries({
    upColor: "#22c55e", downColor: "#ef4444", borderVisible: false, wickUpColor: "#22c55e", wickDownColor: "#ef4444",
  });
  card.candles.priceScale().applyOptions({ scaleMargins: { top: 0.08, bottom: 0.22 } });
  card.volume = card.chart.addHistogramSeries({ priceScaleId: "", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false });
  card.volume.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
}

function renderCards() {
  const st = lastState;
  const order = [...new Set([...(liveInfo?.symbols || []), ...Object.keys(st.symbols || {})])];
  const root = $("#cards");
  if (!order.length) {
    for (const k of Object.keys(cards)) {
      chartObserver?.unobserve(cards[k].box);
      cards[k].chart?.remove();
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
}

// pre-market and after-hours candles are drawn faded, as on most charts
function extColors(up, extended) {
  if (!extended) return {};
  const c = up ? "rgba(34,197,94,.4)" : "rgba(239,68,68,.4)";
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
  $(".rsi", el).textContent = num(v.rsi, 1);
  $(".trend", el).textContent = v.trend || "–";
  $(".trend", el).className = "trend " + (v.trend === "up" ? "up" : v.trend === "down" ? "down" : "");
  const ext = lastState.session && lastState.session !== "regular" ? ` · ${SESSION_NAMES[lastState.session]}` : "";
  $(".bar", el).textContent = v.forming ? `${etTime(new Date(v.forming[0] * 1000))} candle forming${ext}`
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
  const bars = v.bars || [];
  const barKey = bars.length ? `${bars.length}|${bars[bars.length - 1][0]}|${bars[bars.length - 1][4]}` : "";
  if (barKey !== card.keys.bars) {
    card.keys.bars = barKey;
    card.candles.setData(bars.map((b) => ({ time: b[0], open: b[1], high: b[2], low: b[3], close: b[4], ...extColors(b[4] >= b[1], b[6]) })));
    card.volume.setData(bars.map((b) => ({ time: b[0], value: b[5], color: b[4] >= b[1] ? `rgba(34,197,94,${b[6] ? 0.15 : 0.35})` : `rgba(239,68,68,${b[6] ? 0.15 : 0.35})` })));
    if (!card.fitted && bars.length) {
      card.fitted = true;
      setTimeout(() => card.chart.timeScale().fitContent(), 300); // once autoSize has given the chart its width
    }
  }
  // the candle still forming, from the scanner's live checks: it moves with every refresh
  const f = v.forming;
  if (f && (!bars.length || f[0] > bars[bars.length - 1][0])) {
    card.candles.update({ time: f[0], open: f[1], high: f[2], low: f[3], close: f[4],
      ...extColors(f[4] >= f[1], lastState.session && lastState.session !== "regular") });
  }
  // signal markers, snapped to the candle they belong to
  const mkKey = lastSignals.filter((s) => s.symbol === sym).map(signalId).join(",") + barKey;
  if (mkKey !== card.keys.markers) {
    card.keys.markers = mkKey;
    const times = bars.map((b) => b[0]);
    const snap = (t) => { let best = times[0]; for (const x of times) if (x <= t) best = x; return best; };
    const sigs = lastSignals.filter((s) => s.symbol === sym && times.length);
    // one marker per candle and side; only the latest few carry a label, so the chart stays readable
    const groups = new Map();
    for (const s of sigs) {
      const t = snap(Math.floor(new Date(s.time).getTime() / 1000));
      const k = `${t}|${s.side}`;
      const g = groups.get(k) || { time: t, side: s.side, all: [], strong: [] };
      g.all.push(s);
      if (!s.weak) g.strong.push(s);
      groups.set(k, g);
    }
    const list = [...groups.values()].sort((a, b) => a.time - b.time);
    const labelled = new Set(list.filter((g) => g.strong.length).slice(-6));
    const markers = list.map((g) => {
      const buy = g.side === "buy";
      const lead = g.strong[0] || g.all[0];
      const name = lead.live ? "live" : lead.label.length > 16 ? lead.label.slice(0, 15) + "…" : lead.label;
      return {
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
  }
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

function rsiTag(s) {
  // an extreme RSI on the signal's candle holds the other side back (scan.rsi_extreme_low / high)
  if (!s.rsi_extreme || s.rsi == null) return "";
  const what = `RSI ${num(s.rsi, 0)} extremely ${s.rsi_extreme}`;
  return s.held_back
    ? `<span class="tag against" title="Not sent to your phone: ${esc(what)} overrides it">held back · ${esc(what)}</span>`
    : `<span class="tag with" title="Takes priority over the bigger picture">${esc(what)}</span>`;
}

function signalItem(s, isNew) {
  const tags = [s.live ? `<span class="tag live">live</span>` : "", s.weak ? `<span class="tag">log only</span>` : "", effectTag(s), rsiTag(s), contextTag(s)].join("");
  return `<li class="${s.weak ? "weak" : ""}${isNew ? " new" : ""}">
    <div class="line1"><span class="t">${hm(s.time)}</span>${sideBadge(s.side)}<span class="sym">${esc(s.symbol)}</span>
      <span class="lab">${esc(s.label)}</span><span class="px">@ ${num(s.price)}</span>${tags}</div>
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
      <td>${esc(s.label)}${s.live ? ` <span class="tag live">live</span>` : ""}${s.weak ? ` <span class="tag">log only</span>` : ""}</td>
      <td class="mono">${num(s.price)}</td><td class="small">${esc(s.detail || "")}</td><td>${effectTag(s)} ${contextTag(s)}</td></tr>`).join("");
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
    const rows = patData.patterns.filter((p) => p.group === g && (!q || (p.label + p.kind + p.about).toLowerCase().includes(q)));
    if (!rows.length) continue;
    const on = patData.on.includes(g);
    html += `<div class="pat-group-title">${GROUP_NAMES[g] || g} · ${rows.length}${on ? "" : " · group off"}</div>`;
    html += rows.map((p) => `<div class="pat${on ? "" : " groupoff"}">
        <div class="name"><b>${esc(p.label)}</b><span title="${esc(p.about)}">${esc(p.about || p.kind)}</span></div>
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
