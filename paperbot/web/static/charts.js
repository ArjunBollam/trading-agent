"use strict";

// The Charts tab (paperbot/chartdata.py: any interval, chart type and indicator, up to 16 charts, bar replay,
// profiles and footprint) and the Alerts tab (paperbot/alerts.py). Loaded after app.js and uses its helpers
// ($, $$, api, esc, num, toast, when, etTime, fmtVol, loadChartLibrary, ChartLabels, OVERLAYS, smcShapes, smcNotes...).

const CT_STATE_KEY = "ct-state-v1";
const CT_SOURCES = ["close", "open", "high", "low", "hl2", "hlc3", "ohlc4", "hlcc4", "volume"];
const CT_STARTERS = ["SPY", "QQQ", "NVDA", "AAPL", "TSLA", "MSFT", "AMZN", "META", "IWM", "AMD", "GOOGL", "SOXL", "DIA", "TLT", "GLD", "XLF"];
const CT_GRID = { 1: [1, 1], 2: [2, 1], 3: [3, 1], 4: [2, 2], 6: [3, 2], 8: [4, 2], 9: [3, 3], 12: [4, 3], 16: [4, 4] };
const CT_UP = "#089981", CT_DOWN = "#f23645";
const CT_TYPES = { candles: "Candles", hollow: "Hollow candles", bars: "Bars", line: "Line", area: "Area", baseline: "Baseline",
  volume_candles: "Volume candles", heikin: "Heikin Ashi", renko: "Renko", linebreak: "Line break", kagi: "Kagi", pnf: "Point & figure" };
const CT_CANDLE_LIKE = new Set(["candles", "hollow", "bars", "heikin", "renko", "linebreak", "volume_candles"]);
const CT_OFF_CLOCK = new Set(["renko", "linebreak", "kagi", "pnf"]);

const CT = {
  started: false, layout: 1, active: 0, panels: [], views: [], catalog: null, catalogBy: {}, wl: null, quotes: {},
  replay: null, timer: null, quoteTimer: null, applyTimer: null,
};

function ctPanel(symbol) {
  return { symbol, tf: "5m", type: "candles", ext: true, session: "ext", studies: [], sessions: null, box: null, reversal: 3, lines: 3, hl: false,
    ov: { qqe: false, sr: false, sqz: false, smc: false, profile: false, vpvr: false, tpo: false, footprint: false, auto: false, stages: true, positions: true } };
}

function ctLoadState() {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(CT_STATE_KEY) || "null"); } catch { saved = null; }
  CT.layout = CT_GRID[saved?.layout] ? saved.layout : 1;
  CT.active = Number.isInteger(saved?.active) ? saved.active : 0;
  const panels = Array.isArray(saved?.panels) ? saved.panels : [];
  CT.panels = CT_STARTERS.map((s, i) => {
    const p = panels[i];
    if (!p || typeof p.symbol !== "string") return ctPanel(s);
    const base = ctPanel(p.symbol);
    const session = ["rth", "ext", "24h"].includes(p.session) ? p.session : p.ext === false ? "rth" : "ext";
    return { ...base, ...p, session, ext: session !== "rth", ov: { ...base.ov, ...(p.ov || {}) }, studies: Array.isArray(p.studies) ? p.studies : [] };
  });
  if (CT.active >= CT.layout) CT.active = 0;
}

function ctSave() {
  try { localStorage.setItem(CT_STATE_KEY, JSON.stringify({ layout: CT.layout, active: CT.active, panels: CT.panels })); } catch { /* private window */ }
}

const ctActive = () => CT.panels[CT.active];

// how far back a chart loads by default, as the server picks it (paperbot/chartdata.py default_sessions)
function ctDefaultSessions(tf) {
  const m = /^(\d+(?:\.\d+)?)?\s*([a-zA-Z]+)?$/.exec(tf) || [];
  if (/^r/i.test(tf) || /T$/.test(tf) || /s$/.test(tf)) return 1;
  const n = Number(m[1] || 1), u = m[2] || "m";
  if (u === "D" || u === "d") return 5;
  if (u === "W" || u === "w") return 10;
  if (u === "M") return 20;
  const minutes = u === "h" || u === "H" ? n * 60 : n;
  return minutes <= 1 ? 3 : minutes <= 5 ? 5 : minutes <= 30 ? 15 : minutes <= 60 ? 30 : 90;
}
const ctMaxSessions = (tf) => (/^r/i.test(tf) || /T$/.test(tf) || /s$/.test(tf) ? 5 : /[DWM]$/.test(tf) ? 20 : 60);

function ctRequest(p, extra = {}) {
  return {
    symbol: p.symbol, tf: p.tf, type: p.type, ext: p.session !== "rth", session: p.session, sessions: p.sessions, box: p.box,
    reversal: p.reversal, lines: p.lines,
    high_low: p.hl, profile: p.ov.profile, tpo: p.ov.tpo, footprint: p.ov.footprint, auto_lines: !!p.ov.auto,
    overlays: { qqe: p.ov.qqe, sr: p.ov.sr, sqz: p.ov.sqz, smc: p.ov.smc },
    studies: p.studies.map((s) => ({ id: s.id, type: s.type, inputs: s.inputs })), ...extra,
  };
}

/* ---------- custom series: Kagi, point & figure, volume candles (Lightweight Charts custom series API) ---------- */

class CtKagiView {
  constructor() { this.r = { data: null, draw: (target, toY) => this.draw(target, toY) }; }
  priceValueBuilder(d) { return [d.low, d.high, d.end]; }
  isWhitespace(d) { return d.end === undefined; }
  renderer() { return this.r; }
  update(data) { this.r.data = data; }
  defaultOptions() { return { yang: CT_UP, yin: CT_DOWN }; }
  draw(target, toY) {
    const data = this.r.data;
    if (!data?.visibleRange) return;
    target.useBitmapCoordinateSpace(({ context: ctx, horizontalPixelRatio: hr, verticalPixelRatio: vr }) => {
      const seg = (x1, y1, x2, y2, thick) => {
        ctx.strokeStyle = thick ? CT_UP : CT_DOWN;
        ctx.lineWidth = (thick ? 3 : 1) * hr;
        ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
      };
      const from = Math.max(data.visibleRange.from - 1, 0), to = Math.min(data.visibleRange.to + 1, data.bars.length);
      for (let i = from; i < to; i++) {
        const b = data.bars[i], d = b.originalData, x = Math.round(b.x * hr);
        const ys = toY(d.start) * vr, ye = toY(d.end) * vr;
        if (i > 0) {
          const p = data.bars[i - 1], pd = p.originalData;
          const endThick = pd.sw == null ? pd.thick : !pd.thick;
          seg(Math.round(p.x * hr), ys, x, ys, endThick);
        }
        if (d.sw == null) seg(x, ys, x, ye, d.thick);
        else {
          const yw = toY(d.sw) * vr;
          seg(x, ys, x, yw, d.thick);
          seg(x, yw, x, ye, !d.thick);
        }
      }
    });
  }
}

class CtPnfView {
  constructor() { this.r = { data: null, draw: (target, toY) => this.draw(target, toY) }; }
  priceValueBuilder(d) { return [d.bottom, d.top + d.box, d.top]; }
  isWhitespace(d) { return d.top === undefined; }
  renderer() { return this.r; }
  update(data) { this.r.data = data; }
  defaultOptions() { return {}; }
  draw(target, toY) {
    const data = this.r.data;
    if (!data?.visibleRange) return;
    target.useBitmapCoordinateSpace(({ context: ctx, horizontalPixelRatio: hr, verticalPixelRatio: vr }) => {
      for (let i = data.visibleRange.from; i < data.visibleRange.to; i++) {
        const b = data.bars[i], d = b.originalData;
        if (!d) continue;
        const boxPx = Math.abs(toY(d.top) - toY(d.top + d.box)) * vr;
        const size = Math.max(Math.min(data.barSpacing * hr * 0.8, boxPx * 0.85), 2);
        const x = b.x * hr;
        ctx.strokeStyle = d.up ? CT_UP : CT_DOWN;
        ctx.lineWidth = Math.max(1, hr * (size > 10 ? 1.5 : 1));
        for (let lvl = d.bottom; lvl <= d.top + 1e-9; lvl += d.box) {
          const y = toY(lvl + d.box / 2) * vr;
          ctx.beginPath();
          if (d.up) {
            ctx.moveTo(x - size / 2, y - size / 2); ctx.lineTo(x + size / 2, y + size / 2);
            ctx.moveTo(x + size / 2, y - size / 2); ctx.lineTo(x - size / 2, y + size / 2);
          } else {
            ctx.ellipse(x, y, size / 2, size / 2, 0, 0, Math.PI * 2);
          }
          ctx.stroke();
        }
      }
    });
  }
}

class CtVolumeCandleView {
  constructor() { this.r = { data: null, draw: (target, toY) => this.draw(target, toY) }; }
  priceValueBuilder(d) { return [d.low, d.high, d.close]; }
  isWhitespace(d) { return d.close === undefined; }
  renderer() { return this.r; }
  update(data) { this.r.data = data; }
  defaultOptions() { return {}; }
  draw(target, toY) {
    const data = this.r.data;
    if (!data?.visibleRange) return;
    target.useBitmapCoordinateSpace(({ context: ctx, horizontalPixelRatio: hr, verticalPixelRatio: vr }) => {
      let most = 0;
      for (let i = data.visibleRange.from; i < data.visibleRange.to; i++) most = Math.max(most, data.bars[i].originalData.volume || 0);
      for (let i = data.visibleRange.from; i < data.visibleRange.to; i++) {
        const b = data.bars[i], d = b.originalData;
        const up = d.close >= d.open;
        const w = Math.max(1, data.barSpacing * hr * (0.15 + 0.8 * (most ? (d.volume || 0) / most : 0.5)));
        const x = b.x * hr;
        ctx.fillStyle = ctx.strokeStyle = up ? CT_UP : CT_DOWN;
        ctx.lineWidth = Math.max(1, hr);
        ctx.beginPath(); ctx.moveTo(x, toY(d.high) * vr); ctx.lineTo(x, toY(d.low) * vr); ctx.stroke();
        const y1 = toY(Math.max(d.open, d.close)) * vr, y2 = toY(Math.min(d.open, d.close)) * vr;
        ctx.fillRect(x - w / 2, y1, w, Math.max(y2 - y1, hr));
      }
    });
  }
}

/* ---------- profiles, TPO and footprint, drawn over a chart (a series primitive) ---------- */

class CtProfiles {
  constructor(view) {
    this.v = view;
    const pane = (zOrder, draw) => ({ zOrder: () => zOrder, renderer: () => ({ draw }) });
    this.views = [pane("bottom", (t) => this.drawUnder(t)), pane("top", (t) => this.drawOver(t))];
  }
  attached({ chart, series, requestUpdate }) { this.chart = chart; this.series = series; this.requestUpdate = requestUpdate; }
  detached() { this.chart = this.series = this.requestUpdate = null; }
  updateAllViews() {}
  paneViews() { return this.views; }
  update() { this.requestUpdate?.(); }

  drawUnder(target) {
    const d = this.v.data, p = this.v.panel;
    if (!this.chart || !d) return;
    target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
      const ts = this.chart.timeScale(), y = (price) => this.series.priceToCoordinate(price);
      const span = (a, b) => {
        let x1 = ts.timeToCoordinate(a), x2 = ts.timeToCoordinate(b);
        if (x1 == null && x2 == null) return null;
        return [x1 ?? 0, x2 ?? mediaSize.width];
      };
      if (p.ov.profile) {
        for (const pr of d.profiles || []) {
          const s = span(pr.from, pr.to);
          if (!s || s[1] < 0 || s[0] > mediaSize.width) continue;
          ctDrawProfile(ctx, pr.rows, pr.step, pr.vah, pr.val, y, s[0], Math.max((s[1] - s[0]) * 0.35, 24), false);
          const yp = y(pr.poc);
          if (yp != null) { ctx.strokeStyle = "#ff9800"; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(s[0], yp); ctx.lineTo(s[1], yp); ctx.stroke(); }
        }
      }
      if (p.ov.vpvr) {
        const r = ts.getVisibleLogicalRange();
        const rows = r ? ctVisibleProfile(d.bars.slice(Math.max(0, Math.floor(r.from)), Math.ceil(r.to) + 1), 24) : null;
        if (rows) ctDrawProfile(ctx, rows.rows, rows.step, rows.vah, rows.val, y, mediaSize.width, mediaSize.width * 0.22, true, rows.poc);
      }
      if (p.ov.tpo) for (const t of d.tpo || []) ctDrawTpo(ctx, t, y, span(t.from, t.to), mediaSize);
    });
  }

  drawOver(target) {
    const d = this.v.data, p = this.v.panel;
    if (!this.chart || !d || !p.ov.footprint || !d.footprint) return;
    target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
      const ts = this.chart.timeScale(), y = (price) => this.series.priceToCoordinate(price);
      const a = ts.logicalToCoordinate(0), b = ts.logicalToCoordinate(1);
      const spacing = a != null && b != null ? b - a : 0;
      if (spacing < 26) {
        ctx.fillStyle = "rgba(201,204,211,.55)"; ctx.font = "11px -apple-system, sans-serif"; ctx.textAlign = "left";
        ctx.fillText("Zoom in to read the footprint", 8, mediaSize.height - 10);
        return;
      }
      ctx.textAlign = "center"; ctx.textBaseline = "middle";
      for (const [key, f] of Object.entries(d.footprint)) {
        const xc = ts.timeToCoordinate(Number(key));
        if (xc == null || xc < -spacing || xc > mediaSize.width + spacing) continue;
        const w = spacing * 0.92, left = xc - w / 2;
        // rows merged until each is tall enough to read (like TradingView's ticks per row)
        const pxPerStep = Math.abs(y(f.rows[0][0]) - y(f.rows[0][0] + f.step)) || 1;
        const merge = Math.max(1, Math.ceil(13 / pxPerStep));
        const big = f.step * merge;
        const groups = new Map();
        for (const [price, sold, bought] of f.rows) {
          const key = Math.floor(price / big + 1e-9);
          const g = groups.get(key) || [key * big + big / 2 - f.step / 2, 0, 0];
          g[1] += sold; g[2] += bought;
          groups.set(key, g);
        }
        const rows = [...groups.values()];
        const rowH = pxPerStep * merge;
        const most = Math.max(...rows.map((r) => r[1] + r[2]), 1);
        const pocKey = Math.floor(f.poc / big + 1e-9);
        ctx.font = `${Math.max(Math.min(rowH - 3, 11), 7)}px ui-monospace, Menlo, monospace`;
        for (const [price, sold, bought] of rows) {
          const yc = y(price);
          if (yc == null || yc < -rowH || yc > mediaSize.height + rowH) continue;
          const share = (sold + bought) / most;
          ctx.fillStyle = bought >= sold ? `rgba(8,153,129,${0.12 + 0.45 * share})` : `rgba(242,54,69,${0.12 + 0.45 * share})`;
          ctx.fillRect(left, yc - rowH / 2, w, Math.max(rowH - 1, 1));
          if (Math.floor((price + f.step / 2) / big + 1e-9) === pocKey) { ctx.strokeStyle = "#fbc02d"; ctx.lineWidth = 1; ctx.strokeRect(left + 0.5, yc - rowH / 2 + 0.5, w - 1, rowH - 2); }
          if (rowH >= 9 && w >= 40) {
            ctx.fillStyle = bought > 2 * sold && bought > 0 ? "#7ff0c9" : sold > 2 * bought && sold > 0 ? "#ff9aa5" : "#d1d4dc";
            ctx.fillText(`${fmtVol(sold)} x ${fmtVol(bought)}`, xc, yc);
          }
        }
        const low = Math.min(...rows.map((r) => r[0]));
        const yl = y(low);
        if (yl != null) {
          ctx.font = "10px ui-monospace, Menlo, monospace";
          ctx.fillStyle = f.delta >= 0 ? "#26a69a" : "#ef5350";
          ctx.fillText(`Δ ${f.delta >= 0 ? "+" : "−"}${fmtVol(Math.abs(f.delta))}`, xc, yl + rowH + 8);
        }
      }
    });
  }
}

function ctDrawProfile(ctx, rows, step, vah, val, y, x0, width, fromRight, poc) {
  const most = Math.max(...rows.map((r) => r[1] + r[2]), 1);
  for (const [lo, up, dn] of rows) {
    const y1 = y(lo + step), y2 = y(lo);
    if (y1 == null || y2 == null) continue;
    const inside = lo + step / 2 >= val && lo + step / 2 <= vah;
    const h = Math.max(Math.abs(y2 - y1) - 1, 1), top = Math.min(y1, y2);
    const len = (width * (up + dn)) / most, upLen = up + dn ? (len * up) / (up + dn) : 0;
    const a = inside ? 0.55 : 0.25;
    const start = fromRight ? x0 - len : x0;
    ctx.fillStyle = `rgba(8,153,129,${a})`;
    ctx.fillRect(start, top, upLen, h);
    ctx.fillStyle = `rgba(242,54,69,${a})`;
    ctx.fillRect(start + upLen, top, len - upLen, h);
  }
  if (poc != null) {
    const yp = y(poc);
    if (yp != null) { ctx.strokeStyle = "#ff9800"; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(x0 - width, yp); ctx.lineTo(x0, yp); ctx.stroke(); }
  }
}

// the visible candles' volume at each price: each candle's volume spread over its range, split by its direction
function ctVisibleProfile(bars, rowsWanted) {
  const live = bars.filter((b) => b && b[5] > 0);
  if (!live.length) return null;
  const hi = Math.max(...live.map((b) => b[2])), lo = Math.min(...live.map((b) => b[3]));
  if (!(hi > lo)) return null;
  const step = (hi - lo) / rowsWanted;
  const ups = new Array(rowsWanted).fill(0), downs = new Array(rowsWanted).fill(0);
  for (const b of live) {
    const a = Math.min(Math.floor((b[3] - lo) / step), rowsWanted - 1), z = Math.min(Math.floor((b[2] - lo) / step), rowsWanted - 1);
    const each = b[5] / (z - a + 1);
    for (let r = a; r <= z; r++) (b[4] >= b[1] ? ups : downs)[r] += each;
  }
  const tot = ups.map((u, i) => u + downs[i]);
  let p = tot.indexOf(Math.max(...tot)), l = p, h = p, inside = tot[p];
  const all = tot.reduce((s, x) => s + x, 0);
  while (inside < 0.7 * all && (l > 0 || h < rowsWanted - 1)) {
    const below = l > 0 ? tot[l - 1] : -1, above = h < rowsWanted - 1 ? tot[h + 1] : -1;
    if (above >= below) inside += tot[++h]; else inside += tot[--l];
  }
  return { step, rows: tot.map((_, i) => [lo + i * step, ups[i], downs[i]]), poc: lo + (p + 0.5) * step, vah: lo + (h + 1) * step, val: lo + l * step };
}

function ctDrawTpo(ctx, t, y, span, mediaSize) {
  if (!span || span[1] < 0 || span[0] > mediaSize.width) return;
  const [x1, x2] = span;
  const most = Math.max(...t.rows.map((r) => r[1].length), 1);
  const rowH = Math.abs((y(t.rows[0][0]) ?? 0) - (y(t.rows[0][0] + t.step) ?? 0));
  const letters = rowH >= 7 && (x2 - x1) >= most * 6.5;
  ctx.font = `${Math.max(Math.min(rowH - 1, 10), 6)}px ui-monospace, Menlo, monospace`;
  ctx.textBaseline = "middle"; ctx.textAlign = "left";
  const singles = new Set(t.singles.map((s) => s.toFixed(6)));
  for (const [lo, s] of t.rows) {
    if (!s) continue;
    const yc = y(lo + t.step / 2);
    if (yc == null) continue;
    const mid = lo + t.step / 2;
    const isPoc = Math.abs(mid - t.poc) < t.step / 2, inside = mid >= t.val && mid <= t.vah;
    const color = isPoc ? "#fbc02d" : singles.has(lo.toFixed(6)) ? "#ce93d8" : inside ? "#90caf9" : "#5b6473";
    if (letters) {
      ctx.fillStyle = color;
      ctx.fillText(s, x1 + 4, yc);
    } else {
      ctx.fillStyle = color.replace(")", ",.5)").replace("#", "#");
      ctx.globalAlpha = 0.5;
      ctx.fillRect(x1 + 4, yc - rowH / 2, ((x2 - x1) * 0.35 * s.length) / most, Math.max(rowH - 1, 1));
      ctx.globalAlpha = 1;
    }
  }
  const ya = y(t.ib[1]), yb = y(t.ib[0]);
  if (ya != null && yb != null) { ctx.strokeStyle = "#42a5f5"; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(x1 + 1, ya); ctx.lineTo(x1 + 1, yb); ctx.stroke(); }
}

/* ---------- one chart panel ---------- */

function ctView(i) {
  if (!CT.views[i]) {
    const el = document.createElement("div");
    el.className = "ct-panel";
    el.dataset.i = i;
    el.innerHTML = `<div class="ct-legend"><div class="ct-title"><b class="ct-sym"></b><span class="ct-tfl"></span><span class="ct-ohlc"></span></div>
      <div class="ct-studies-legend"></div></div><div class="ct-main"></div><div class="ct-panes"></div>
      <div class="ct-note muted small"></div><div class="ct-loading" hidden>Loading…</div>`;
    el.addEventListener("mousedown", () => ctSetActive(i));
    CT.views[i] = { i, el, main: $(".ct-main", el), panesEl: $(".ct-panes", el), panes: new Map(), studySeries: [], seq: 0, loadedAt: 0 };
  }
  const v = CT.views[i];
  v.panel = CT.panels[i];
  return v;
}

function ctRenderGrid() {
  const [cols, rows] = CT_GRID[CT.layout];
  const grid = $("#ct-grid");
  grid.style.gridTemplateColumns = `repeat(${cols}, minmax(0, 1fr))`;
  grid.style.gridTemplateRows = `repeat(${rows}, minmax(0, 1fr))`;
  grid.dataset.n = CT.layout;
  const want = [];
  for (let i = 0; i < CT.layout; i++) want.push(ctView(i).el);
  if (grid.children.length !== want.length || want.some((el, k) => grid.children[k] !== el)) grid.replaceChildren(...want);
  for (let i = CT.layout; i < CT.views.length; i++) if (CT.views[i]) { ctDestroy(CT.views[i]); CT.views[i].data = null; }
  want.forEach((el, k) => el.classList.toggle("active", k === CT.active && CT.layout > 1));
}

function ctDestroy(v) {
  if (v.chart) { v.chart.remove(); v.chart = null; }
  for (const p of v.panes.values()) p.chart.remove();
  v.panes.clear();
  v.panesEl.replaceChildren();
  v.structure = null;
}

function ctSetActive(i) {
  if (CT.active === i) return;
  CT.active = i;
  ctSave();
  $$("#ct-grid .ct-panel").forEach((el) => el.classList.toggle("active", Number(el.dataset.i) === i && CT.layout > 1));
  ctSyncToolbar();
}

const ctPrecision = (price) => (Math.abs(price) < 1 ? 5 : Math.abs(price) < 10 ? 4 : 2);

function ctTimeText(v, x, long = true) {
  const real = v.real?.get(x) ?? x;
  const d = v.data;
  if (d && !d.intraday && !d.synthetic) return new Date(real * 1000).toLocaleDateString("en-US", { timeZone: ET, month: "short", day: "numeric", year: "numeric" });
  const sec = d && (d.seconds != null ? d.seconds < 60 : /T$|^R/.test(d.tf));
  const t = new Date(real * 1000);
  const time = t.toLocaleTimeString("en-US", { timeZone: ET, hour12: false, hour: "2-digit", minute: "2-digit", ...(sec ? { second: "2-digit" } : {}) });
  return long && etDate(real) !== etDate(Date.now() / 1000) ? `${t.toLocaleDateString("en-US", { timeZone: ET, weekday: "short", month: "short", day: "numeric" })} ${time}` : time;
}

function ctMakeChart(v) {
  const theme = {
    autoSize: true,
    layout: { background: { type: "solid", color: "#0f0f0f" }, textColor: "#b2b5be", fontSize: 11 },
    grid: { vertLines: { color: "rgba(255,255,255,.05)" }, horzLines: { color: "rgba(255,255,255,.05)" } },
    rightPriceScale: { borderColor: "#2a2e39", minimumWidth: 72 },
    timeScale: { borderColor: "#2a2e39", timeVisible: true, secondsVisible: false, rightOffset: 8,
      tickMarkFormatter: (t, type) => {
        const real = v.real?.get(t) ?? t;
        if (v.data && !v.data.intraday && !v.data.synthetic) return new Date(real * 1000).toLocaleDateString("en-US", { timeZone: ET, month: "short", ...(type < 2 ? {} : { day: "numeric" }), ...(type === 0 ? { year: "numeric" } : {}) });
        // where the chart moves to a new day: the date of that candle (in ET), otherwise its time
        return !v.data?.synthetic && (type < 3 || v.dayStart?.has(t)) ? new Date(real * 1000).toLocaleDateString("en-US", { timeZone: ET, month: "short", day: "numeric" }) : ctTimeText(v, t, false);
      } },
    localization: { timeFormatter: (t) => ctTimeText(v, t) },
    crosshair: { mode: 0 },
  };
  return LightweightCharts.createChart(v.main, theme);
}

function ctMainSeries(v, type) {
  const c = v.chart;
  const fp = v.panel.ov.footprint;
  const up = fp ? "rgba(8,153,129,.3)" : CT_UP, down = fp ? "rgba(242,54,69,.3)" : CT_DOWN;
  if (type === "bars") return c.addBarSeries({ upColor: up, downColor: down, thinBars: false });
  if (type === "line") return c.addLineSeries({ color: "#2962ff", lineWidth: 2 });
  if (type === "area") return c.addAreaSeries({ lineColor: "#2962ff", topColor: "rgba(41,98,255,.35)", bottomColor: "rgba(41,98,255,0)", lineWidth: 2 });
  if (type === "baseline") return c.addBaselineSeries({ baseValue: { type: "price", price: 0 }, lineWidth: 2 });
  if (type === "kagi") return c.addCustomSeries(new CtKagiView(), { priceLineVisible: false });
  if (type === "pnf") return c.addCustomSeries(new CtPnfView(), { priceLineVisible: false });
  if (type === "volume_candles") return c.addCustomSeries(new CtVolumeCandleView(), {});
  if (type === "hollow") return c.addCandlestickSeries({ upColor: "rgba(0,0,0,0)", downColor: down, borderVisible: true, borderUpColor: up, borderDownColor: down, wickUpColor: up, wickDownColor: down });
  return c.addCandlestickSeries({ upColor: up, downColor: down, borderVisible: false, wickUpColor: up, wickDownColor: down });
}

function ctStructureKey(p, d) {
  return JSON.stringify([d.type, d.synthetic, d.tf, (d.studies || []).map((s) => [s.id, s.pane, s.outputs.map((o) => o.key + o.kind)]), p.ov.footprint]);
}

function ctBuild(v) {
  ctDestroy(v);
  const d = v.data;
  v.chart = ctMakeChart(v);
  v.series = ctMainSeries(v, d.type);
  v.volume = CT_CANDLE_LIKE.has(d.type) || ["line", "area", "baseline"].includes(d.type)
    ? v.chart.addHistogramSeries({ priceScaleId: "", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false }) : null;
  v.volume?.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
  v.series.priceScale().applyOptions({ scaleMargins: { top: 0.1, bottom: v.volume ? 0.2 : 0.08 } });
  v.labels = new ChartLabels();
  v.series.attachPrimitive(v.labels);
  v.profiles = new CtProfiles(v);
  v.series.attachPrimitive(v.profiles);
  v.draw = new CtDrawings(v);
  v.series.attachPrimitive(v.draw);
  v.posLines = [];
  ctdWire(v);
  v.studySeries = [];
  const panes = [];
  for (const s of d.studies || []) if (s.pane !== "price" && !panes.includes(s.pane)) panes.push(s.pane);
  for (const id of panes) {
    const el = document.createElement("div");
    el.className = "ct-pane";
    el.innerHTML = `<div class="ct-pane-legend"></div><div class="ct-pane-chart"></div>`;
    v.panesEl.append(el);
    const chart = LightweightCharts.createChart($(".ct-pane-chart", el), {
      autoSize: true, layout: { background: { type: "solid", color: "#0f0f0f" }, textColor: "#b2b5be", fontSize: 10 },
      grid: { vertLines: { color: "rgba(255,255,255,.04)" }, horzLines: { color: "rgba(255,255,255,.04)" } },
      rightPriceScale: { borderColor: "#2a2e39", minimumWidth: 72 }, timeScale: { visible: false, rightOffset: 8 }, crosshair: { mode: 0 },
      handleScroll: true, handleScale: true,
    });
    v.panes.set(id, { el, chart, legend: $(".ct-pane-legend", el), series: [] });
  }
  for (const s of d.studies || []) {
    const target = s.pane === "price" ? { chart: v.chart } : v.panes.get(s.pane);
    s.outputs.forEach((o, k) => {
      const opts = { color: o.color, lineWidth: o.kind === "line" ? 1 : 1, priceLineVisible: false, lastValueVisible: s.pane !== "price" || k === 0, crosshairMarkerVisible: false };
      let series;
      if (o.kind === "hist") series = target.chart.addHistogramSeries(o.key === "volume" ? { ...opts, priceFormat: { type: "volume" } } : opts);
      else if (o.kind === "dots") series = target.chart.addLineSeries({ ...opts, lineVisible: false, pointMarkersVisible: true, pointMarkersRadius: 1.5 });
      else series = target.chart.addLineSeries(opts);
      if (k === 0 && s.pane !== "price") for (const lvl of s.levels) series.createPriceLine({ price: lvl, color: "rgba(178,181,190,.35)", lineWidth: 1, lineStyle: 2, axisLabelVisible: false });
      v.studySeries.push({ study: s, out: o, series, pane: s.pane });
      if (s.pane !== "price") target.series.push(series);
    });
  }
  // the panes follow the price chart's scrolling and zoom, and its crosshair
  let syncing = false;
  const follow = (from) => (r) => {
    if (syncing || !r) return;
    syncing = true;
    for (const c of [v.chart, ...[...v.panes.values()].map((p) => p.chart)]) if (c !== from) c.timeScale().setVisibleLogicalRange(r);
    syncing = false;
  };
  v.chart.timeScale().subscribeVisibleLogicalRangeChange(follow(v.chart));
  for (const p of v.panes.values()) p.chart.timeScale().subscribeVisibleLogicalRangeChange(follow(p.chart));
  v.chart.timeScale().subscribeVisibleLogicalRangeChange(() => { if (v.panel.ov.vpvr) v.profiles.update(); });
  v.chart.subscribeCrosshairMove((param) => {
    v.hover = param.point && param.time != null ? param.time : null;
    ctLegend(v);
    for (const p of v.panes.values()) {
      if (!p.chart.setCrosshairPosition) continue;
      if (v.hover == null || !p.series[0]) p.chart.clearCrosshairPosition();
      else {
        const s = v.studySeries.find((x) => x.series === p.series[0]);
        const val = s && ctValueAt(v, s.out, v.hover);
        if (val != null) p.chart.setCrosshairPosition(val, v.hover, p.series[0]); else p.chart.clearCrosshairPosition();
      }
    }
  });
  v.chart.subscribeClick((param) => ctClick(v, param));
  v.structure = ctStructureKey(v.panel, d);
}

function ctValueAt(v, out, x) {
  const k = v.index?.get(x);
  return k == null ? null : out.values[k];
}

function ctFill(v, keepView) {
  const d = v.data;
  const p = v.panel;
  if (!v.chart || v.structure !== ctStructureKey(p, d)) { ctBuild(v); keepView = false; }
  const range = keepView ? v.chart.timeScale().getVisibleLogicalRange() : null;
  const bars = d.bars;
  v.real = new Map(bars.map((b) => [b[0], b[7]]));
  v.dayStart = new Set(bars.filter((b, k) => k && etDate(b[7]) !== etDate(bars[k - 1][7])).map((b) => b[0]));
  v.index = new Map(bars.map((b, k) => [b[0], k]));
  v.clk = null; // the market clock at each candle, worked out again when a line is drawn
  v.steps = null;
  if (d.holidays && JSON.stringify(d.holidays) !== JSON.stringify(CT.holidays)) { CT.holidays = d.holidays; CT.holidayStarts = {}; }
  const last = bars.at(-1);
  const prec = last ? ctPrecision(last[4]) : 2;
  v.series.applyOptions({ priceFormat: { type: "price", precision: prec, minMove: 10 ** -prec } });
  const faded = (up) => (up ? "rgba(8,153,129,.45)" : "rgba(242,54,69,.45)");
  const ext = (b) => d.intraday && !d.synthetic && b[6] === 1 && !p.ov.footprint;
  let rows;
  if (d.type === "kagi") {
    rows = (d.columns || []).map((c) => ({ time: c[0], start: c[1], end: c[2], thick: !!c[3], sw: c[4], high: Math.max(c[1], c[2]), low: Math.min(c[1], c[2]) }));
  } else if (d.type === "pnf") {
    rows = (d.columns || []).map((c) => ({ time: c[0], up: !!c[1], top: c[2], bottom: c[3], box: d.box }));
  } else if (["line", "area", "baseline"].includes(d.type)) {
    rows = bars.map((b) => ({ time: b[0], value: b[4] }));
    if (d.type === "baseline" && bars.length) v.series.applyOptions({ baseValue: { type: "price", price: bars[0][4] } });
  } else if (d.type === "volume_candles") {
    rows = bars.map((b) => ({ time: b[0], open: b[1], high: b[2], low: b[3], close: b[4], volume: b[5] }));
  } else {
    rows = bars.map((b) => {
      const r = { time: b[0], open: b[1], high: b[2], low: b[3], close: b[4] };
      if (ext(b)) { const c = faded(b[4] >= b[1]); Object.assign(r, { color: c, wickColor: c, borderColor: c }); }
      return r;
    });
  }
  v.series.setData(rows);
  if (v.volume) {
    v.volume.setData(bars.map((b) => {
      const iex = d.iex_from && b[7] >= d.iex_from;
      const a = iex || ext(b) ? 0.22 : 0.45;
      return { time: b[0], value: b[5], color: b[4] >= b[1] ? `rgba(8,153,129,${a})` : `rgba(242,54,69,${a})` };
    }));
  }
  for (const s of v.studySeries) {
    const colorFor = (val, k) => {
      if (s.out.kind !== "hist") return undefined;
      if (s.out.key === "volume") return bars[k][4] >= bars[k][1] ? "rgba(8,153,129,.5)" : "rgba(242,54,69,.5)";
      const prev = k ? s.out.values[k - 1] : null;
      return val >= 0 ? (prev != null && val < prev ? "#b2dfdb" : "#26a69a") : (prev != null && val > prev ? "#ffcdd2" : "#ef5350");
    };
    s.series.setData(bars.map((b, k) => {
      const val = s.out.values[k];
      if (val == null) return { time: b[0] };
      const c = colorFor(val, k);
      return c ? { time: b[0], value: val, color: c } : { time: b[0], value: val };
    }));
  }
  for (const pane of v.panes.values()) pane.chart.applyOptions({ timeScale: { rightOffset: 8 } });
  ctPaintOverlays(v);
  v.profiles.update();
  if (range) v.chart.timeScale().setVisibleLogicalRange(range);
  else if (bars.length) {
    const show = Math.min(bars.length, CT.layout > 4 ? 80 : 160);
    v.chart.timeScale().setVisibleLogicalRange({ from: bars.length - show, to: bars.length + 6 });
  }
  ctdFetch(p.symbol);
  ctdPositions(v);
  v.draw.update();
  ctLegend(v);
}

// QQE, S/R breaks, squeeze and smart money, drawn as on the dashboard (app.js OVERLAYS, smcShapes)
function ctPaintOverlays(v) {
  const d = v.data, p = v.panel;
  if (!v.labels) return;
  const ov = d.overlays || {};
  if (!CT_CANDLE_LIKE.has(d.type) && !["line", "area", "baseline"].includes(d.type)) { v.labels.set([], [], []); return; }
  const bars = d.bars;
  const byTime = new Map(bars.map((b) => [b[0], b]));
  const shown = [];
  for (const id of ["qqe", "sr", "sqz"]) if (p.ov[id] && ov[id]) shown.push(...OVERLAYS[id].signals(ov).filter((s) => byTime.has(s.t)));
  shown.push(...ctdStageLabels(v, byTime), ...ctgLabels(v));
  const used = new Map();
  const labels = shown.sort((a, b) => a.t - b.t).map((s) => {
    const below = s.side === "long";
    const at = `${s.t}|${below}`;
    const room = used.get(at) ?? 2;
    used.set(at, room + LABEL_HEIGHT);
    return { t: s.t, price: byTime.get(s.t)[below ? 3 : 2], below, name: s.name, color: s.color, room };
  });
  v.notes = new Map();
  for (const s of shown) v.notes.set(s.t, [...(v.notes.get(s.t) || []), s]);
  let smc = { lines: [], boxes: [] };
  if (p.ov.smc && ov.smc) {
    const was = showOverlay.smc;
    showOverlay.smc = true;
    try {
      smc = smcShapes(ov, bars);
      for (const x of smcNotes(ov)) v.notes.set(x.t, [...(v.notes.get(x.t) || []), x]);
    } finally { showOverlay.smc = was; }
  }
  const lines = [];
  if (p.ov.sr && ov.sr) {
    for (const [k, color] of [[1, "rgba(239,68,68,.75)"], [2, "rgba(59,130,246,.8)"]]) {
      const at = ov.sr.levels.filter((l) => byTime.has(l[0]));
      at.forEach((l, i) => {
        if (l[k] == null || (i && at[i - 1][k] === l[k])) return;
        const next = at.slice(i + 1).find((m) => m[k] !== l[k]);
        const to = next ? bars[v.index.get(next[0]) - 1]?.[0] : bars.at(-1)?.[0];
        if (to != null) lines.push({ from: l[0], to, price: l[k], color });
      });
    }
  }
  v.labels.set(labels, [...smc.lines, ...lines], smc.boxes);
}

function ctLegend(v) {
  const d = v.data, p = v.panel;
  const el = v.el;
  $(".ct-sym", el).textContent = p.symbol;
  const boxText = d?.box ? ` (${d.type === "kagi" ? "reversal" : "box"} ${num(d.box, d.box < 0.1 ? 4 : 2)})` : "";
  $(".ct-tfl", el).textContent = d ? ` · ${d.tf} · ${CT_TYPES[d.type]}${boxText}` : ` · ${p.tf}`;
  if (!d || !d.bars.length) { $(".ct-ohlc", el).textContent = ""; $(".ct-studies-legend", el).innerHTML = ""; return; }
  const k = v.hover != null && v.index.has(v.hover) ? v.index.get(v.hover) : d.bars.length - 1;
  const b = d.bars[k], prev = d.bars[k - 1];
  const prec = ctPrecision(b[4]);
  const chg = prev ? b[4] - prev[4] : 0;
  const cls = b[4] >= b[1] ? "up" : "down";
  const col = d.type === "kagi" || d.type === "pnf" ? d.columns?.[k] : null;
  $(".ct-ohlc", el).innerHTML = col
    ? ` <span class="muted">${esc(ctTimeText(v, b[0]))}</span> ${d.type === "kagi" ? `from <b>${num(col[1], prec)}</b> to <b class="${col[2] >= col[1] ? "up" : "down"}">${num(col[2], prec)}</b>`
      : `${col[1] ? "X" : "O"} <b>${num(col[3], prec)}</b> – <b>${num(col[2], prec)}</b>`}`
    : ` <span class="muted">${esc(ctTimeText(v, b[0]))}</span> O <b class="${cls}">${num(b[1], prec)}</b> H <b class="${cls}">${num(b[2], prec)}</b> L <b class="${cls}">${num(b[3], prec)}</b> C <b class="${cls}">${num(b[4], prec)}</b>` +
      ` <span class="${chg >= 0 ? "up" : "down"}">${chg >= 0 ? "+" : ""}${num(chg, prec)}${prev && prev[4] ? ` (${chg >= 0 ? "+" : ""}${num((chg / prev[4]) * 100, 2)}%)` : ""}</span>` +
      (b[5] ? ` V <b>${fmtVol(b[5])}</b>` : "");
  const notes = v.hover != null ? v.notes?.get(v.hover) || [] : [];
  const priceStudies = (d.studies || []).filter((s) => s.pane === "price");
  $(".ct-studies-legend", el).innerHTML = priceStudies.map((s) => ctStudyLegend(s, k)).join("") +
    notes.map((n) => `<div class="ct-note-line" style="color:${n.color}">${esc(n.name)}: <span class="muted">${esc(n.text)}</span></div>`).join("");
  for (const [id, pane] of v.panes) {
    pane.legend.innerHTML = (d.studies || []).filter((s) => s.pane === id).map((s) => ctStudyLegend(s, k)).join("");
  }
}

function ctStudyLegend(s, k) {
  const vals = s.outputs.map((o) => {
    const val = o.values[k];
    return `<span style="color:${o.color}">${val == null ? "∅" : Math.abs(val) >= 1e5 ? fmtVol(val) : num(val, Math.abs(val) < 10 ? 3 : 2)}</span>`;
  }).join(" ");
  return `<div class="ct-study-line">${esc(s.name)} ${vals}</div>`;
}

async function ctLoad(i, { keepView = true, quiet = false } = {}) {
  const v = ctView(i);
  const p = v.panel;
  const seq = ++v.seq;
  v.inflight = true;
  const replaying = CT.replay && CT.replay.i === i && CT.replay.until;
  if (!quiet) $(".ct-loading", v.el).hidden = false;
  try {
    await loadChartLibrary();
    const d = await api("/api/chart-data", { body: ctRequest(p, replaying ? { until: CT.replay.until } : {}) });
    if (seq !== v.seq) return;
    v.data = d;
    v.loadedAt = Date.now();
    v.error = null;
    ctFill(v, keepView);
    const notes = [...(d.notes || [])];
    if (d.iex_from) notes.push(`Volume from ${etTime(new Date(d.iex_from * 1000))} on is IEX's share only (the whole market's comes 15 minutes late on the free data plan).`);
    if (!d.bars.length) notes.push("No candles for this symbol and interval.");
    $(".ct-note", v.el).innerHTML = [`<span title="Data source">${esc(d.source)}</span>`, ...notes.map(esc),
      ...ctdLineNotes(v).map((x) => `<b>${esc(x)}</b>`)].join(" · ");
    if (replaying) ctReplayStatus();
  } catch (e) {
    if (seq !== v.seq) return;
    v.error = e.message;
    v.loadedAt = Date.now(); // a chart that fails waits a minute before it tries again
    $(".ct-note", v.el).innerHTML = `<span class="err-text">${esc(e.message)}</span>`;
  } finally {
    if (seq === v.seq) { $(".ct-loading", v.el).hidden = true; v.inflight = false; }
  }
}

function ctLoadAll() {
  for (let i = 0; i < CT.layout; i++) ctLoad(i, { keepView: false });
}

// live: each chart again every few seconds while the tab is open (not while replaying it)
function ctTick() {
  if (activeTab() !== "charts" || document.hidden) return;
  const now = Date.now();
  for (let i = 0; i < CT.layout; i++) {
    const v = CT.views[i];
    if (!v || !v.data || v.inflight || (CT.replay && CT.replay.i === i)) continue; // never cut short a load still coming
    const daily = /[DWM]$/.test(v.data.tf);
    const every = v.error || daily ? 60000 : CT.layout <= 2 ? 5000 : CT.layout <= 6 ? 10000 : 20000;
    if (now - v.loadedAt >= every) ctLoad(i, { quiet: true });
  }
}

/* ---------- the toolbar ---------- */

function ctSyncToolbar() {
  const p = ctActive();
  $("#ct-symbol").value = p.symbol;
  const known = [...$("#ct-tf").options].some((o) => o.value === p.tf);
  $("#ct-tf").value = known ? p.tf : "custom";
  $("#ct-tf-custom").hidden = known;
  if (!known) $("#ct-tf-custom").value = p.tf;
  $("#ct-type").value = p.type;
  $("#ct-session").value = p.session;
  $("#ct-layout").value = String(CT.layout);
  $("#ct-study-count").textContent = p.studies.length;
  for (const box of $$("#ct-draw input[data-ov]")) box.checked = !!p.ov[box.dataset.ov];
  ctTypeOptions();
}

function ctTypeOptions() {
  const p = ctActive();
  const el = $("#ct-type-opts");
  const box = (label, title) => `<label title="${title}">${label} <input type="number" step="any" min="0" data-opt="box" value="${p.box ?? ""}" placeholder="ATR" size="5"></label>`;
  el.innerHTML = p.type === "renko" ? box("Box", "Brick size; empty: ATR(14) of the candles, rounded")
    : p.type === "kagi" ? box("Reversal", "How far price must come back to turn the line; empty: ATR(14)")
      : p.type === "pnf" ? box("Box", "Box size; empty: ATR(14)") + `<label title="Boxes against the column to start a new one">Reversal <input type="number" min="1" max="10" data-opt="reversal" value="${p.reversal}" size="2"></label>
        <label title="Use highs and lows instead of closes"><input type="checkbox" data-opt="hl" ${p.hl ? "checked" : ""}> High/low</label>`
        : p.type === "linebreak" ? `<label title="Lines a close must pass to turn">Lines <input type="number" min="1" max="10" data-opt="lines" value="${p.lines}" size="2"></label>` : "";
}

function ctApply(change, { reload = true, keepView = false } = {}) {
  Object.assign(ctActive(), change);
  ctSave();
  ctSyncToolbar();
  if (CT.replay && CT.replay.i === CT.active && ("symbol" in change || "tf" in change || "type" in change)) ctReplayExit(false);
  if (reload) ctLoad(CT.active, { keepView });
}

function ctWireToolbar() {
  $("#ct-symbol-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const s = $("#ct-symbol").value.trim().toUpperCase();
    if (s) ctApply({ symbol: s, sessions: null });
  });
  $("#ct-symbol").addEventListener("change", () => {
    const s = $("#ct-symbol").value.trim().toUpperCase();
    if (s && s !== ctActive().symbol) ctApply({ symbol: s, sessions: null });
  });
  $("#ct-tf").addEventListener("change", () => {
    const tf = $("#ct-tf").value;
    $("#ct-tf-custom").hidden = tf !== "custom";
    if (tf === "custom") { $("#ct-tf-custom").focus(); return; }
    ctApply({ tf, sessions: null });
  });
  $("#ct-tf-custom").addEventListener("change", () => {
    const tf = $("#ct-tf-custom").value.trim();
    if (tf) ctApply({ tf, sessions: null });
  });
  $("#ct-type").addEventListener("change", () => ctApply({ type: $("#ct-type").value, box: null }));
  $("#ct-type-opts").addEventListener("change", (e) => {
    const k = e.target.dataset.opt;
    if (!k) return;
    const val = e.target.type === "checkbox" ? e.target.checked : e.target.value === "" ? null : Number(e.target.value);
    ctApply({ [k]: val });
  });
  $("#ct-session").addEventListener("change", () => {
    const session = $("#ct-session").value, p = ctActive();
    const m = /^(\d+)m$/.exec(p.tf), h = /^\d+h$/.test(p.tf);
    // the 24-hour market's candles come in 5-minute steps
    const tf = session === "24h" && !h && !m && !/[DWM]$/.test(p.tf) ? "5m" : p.tf; // seconds, ticks, ranges: IEX until 20:00
    ctApply({ session, ext: session !== "rth", tf }, { keepView: false });
  });
  $("#ct-draw").addEventListener("change", (e) => {
    const k = e.target.dataset.ov;
    if (!k) return;
    const p = ctActive();
    p.ov = { ...p.ov, [k]: e.target.checked };
    ctSave();
    const v = CT.views[CT.active];
    if ((["vpvr", "stages", "positions"].includes(k) || (k === "smc" && v?.data?.overlays?.smc) || (["qqe", "sr", "sqz"].includes(k) && v?.data?.overlays?.[k])) && v?.data) {
      ctPaintOverlays(v); v.profiles?.update(); ctdPositions(v); return; // already loaded: just draw or hide it
    }
    ctLoad(CT.active, { keepView: true });
  });
  $("#ct-layout").addEventListener("change", () => {
    CT.layout = Number($("#ct-layout").value);
    if (CT.active >= CT.layout) CT.active = 0;
    if (CT.replay && CT.replay.i >= CT.layout) ctReplayExit(false);
    ctSave();
    ctRenderGrid();
    for (let i = 0; i < CT.layout; i++) if (!CT.views[i].data) ctLoad(i, { keepView: false });
  });
  $("#ct-more").addEventListener("click", () => {
    const p = ctActive();
    const now = p.sessions || ctDefaultSessions(p.tf), max = ctMaxSessions(p.tf);
    if (now >= max) { toast(`That's as far back as this interval goes (${max} ${/[DWM]$/.test(p.tf) ? "years" : "sessions"})`); return; }
    ctApply({ sessions: Math.min(now * 2, max) }, { keepView: true });
    toast(`Loading ${Math.min(now * 2, max)} ${/[DWM]$/.test(p.tf) ? "years" : "sessions"}…`);
  });
  $("#ct-export").addEventListener("click", ctExport);
  $("#ct-alert").addEventListener("click", () => {
    const v = CT.views[CT.active], p = ctActive();
    if (!/^[A-Z][A-Z0-9.]{0,9}$/.test(p.symbol)) { toast("Alerts are on one symbol, not a formula", true); return; }
    const last = v?.data?.bars?.at(-1)?.[4];
    alPrefill({ symbols: [p.symbol], tf: /^\d+m$|^\d+h$|^1D$/.test(p.tf) ? p.tf : "5m", price: last, overnight: p.session === "24h" });
  });
  $("#ct-watch-toggle").addEventListener("click", () => {
    const hidden = !$("#ct-watch").hidden;
    $("#ct-watch").hidden = hidden;
    remember("ct-watch-hidden", hidden ? "1" : "");
  });
  $("#ct-studies-open").addEventListener("click", ctOpenStudies);
  document.addEventListener("mousedown", (e) => { if (!e.target.closest("#ct-draw")) $("#ct-draw").open = false; });
  $("#ct-replay-open").addEventListener("click", ctReplayStart);
}

async function ctExport() {
  const p = ctActive();
  try {
    const r = await fetch("/api/chart-data?format=csv", { method: "POST", headers: { "Content-Type": "application/json", "X-Paperbot-Token": TOKEN },
      body: JSON.stringify(ctRequest(p, CT.replay?.i === CT.active && CT.replay.until ? { until: CT.replay.until } : {})) });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
    const blob = await r.blob();
    const name = /filename="([^"]+)"/.exec(r.headers.get("content-disposition") || "")?.[1] || "chart.csv";
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  } catch (e) { toast("Export failed: " + e.message, true); }
}

/* ---------- indicators dialog ---------- */

function ctOpenStudies() {
  $("#ct-studies-for").textContent = `${ctActive().symbol} · ${ctActive().tf}`;
  ctRenderCatalog();
  ctRenderChosen();
  $("#ct-studies").showModal();
  $("#ct-study-search").focus();
}

function ctRenderCatalog() {
  const q = $("#ct-study-search").value.trim().toLowerCase();
  const groups = {};
  for (const s of CT.catalog || []) {
    if (q && !`${s.name} ${s.type} ${s.group}`.toLowerCase().includes(q)) continue;
    (groups[s.group] ||= []).push(s);
  }
  $("#ct-catalog").innerHTML = Object.entries(groups).map(([g, list]) => `<div class="ct-cat-group">${esc(g)}</div>` +
    list.map((s) => `<button type="button" class="ct-cat" data-type="${esc(s.type)}" title="${esc(s.about || s.name)}">${esc(s.name)}<span class="muted small">${s.overlay ? "on price" : "own pane"}</span></button>`).join("")).join("") ||
    `<div class="muted">No indicator matches.</div>`;
}

function ctStudyName(s) {
  const def = CT.catalogBy[s.type];
  if (!def) return s.type;
  const nums = def.inputs.filter((x) => x.kind !== "source").map((x) => s.inputs[x.key] ?? x.default);
  return [def.name, ...nums].join(" ");
}

function ctRenderChosen() {
  const p = ctActive();
  $("#ct-study-count").textContent = p.studies.length;
  if (!p.studies.length) { $("#ct-chosen").innerHTML = `<div class="muted ct-empty">Pick indicators on the left.</div>`; return; }
  $("#ct-chosen").innerHTML = p.studies.map((s, k) => {
    const def = CT.catalogBy[s.type];
    if (!def) return "";
    const earlier = p.studies.slice(0, k).flatMap((e) => (CT.catalogBy[e.type]?.outputs || []).map((o) => [`${e.id}:${o.key}`, `${ctStudyName(e)} ${CT.catalogBy[e.type].outputs.length > 1 ? o.name : ""}`.trim()]));
    const fields = def.inputs.map((x) => {
      const val = s.inputs[x.key] ?? x.default;
      if (x.kind === "source") {
        const opts = [...CT_SOURCES.map((c) => [c, c]), ...earlier];
        return `<label>${esc(x.name)} <select data-k="${k}" data-in="${esc(x.key)}">${opts.map(([v, n]) => `<option value="${esc(v)}" ${v === val ? "selected" : ""}>${esc(n)}</option>`).join("")}</select></label>`;
      }
      return `<label>${esc(x.name)} <input type="number" data-k="${k}" data-in="${esc(x.key)}" value="${val}" min="${x.min}" max="${x.max}" step="${x.kind === "int" ? 1 : "any"}"></label>`;
    }).join("");
    return `<div class="ct-chosen-row"><div class="ct-chosen-head"><b>${esc(ctStudyName(s))}</b><span class="muted small">${def.overlay ? "on price" : "own pane"}</span>
      <button type="button" class="ghost ct-x" data-remove="${k}" title="Remove">✕</button></div><div class="ct-inputs">${fields}</div></div>`;
  }).join("");
}

function ctStudiesChanged() {
  ctSave();
  clearTimeout(CT.applyTimer);
  CT.applyTimer = setTimeout(() => ctLoad(CT.active, { keepView: true }), 350);
}

function ctWireStudies() {
  $("#ct-study-search").addEventListener("input", ctRenderCatalog);
  $("#ct-studies-close").addEventListener("click", () => $("#ct-studies").close());
  $("#ct-catalog").addEventListener("click", (e) => {
    const t = e.target.closest("[data-type]")?.dataset.type;
    if (!t) return;
    const p = ctActive();
    if (p.studies.length >= 50) { toast("At most 50 indicators on a chart", true); return; }
    let n = 1;
    while (p.studies.some((s) => s.id === `${t}${n}`)) n++;
    p.studies.push({ id: `${t}${n}`, type: t, inputs: {} });
    ctRenderChosen();
    ctStudiesChanged();
  });
  $("#ct-chosen").addEventListener("click", (e) => {
    const k = e.target.dataset.remove;
    if (k == null) return;
    const p = ctActive();
    const [gone] = p.studies.splice(Number(k), 1);
    for (const s of p.studies) if (String(s.inputs.source || "").startsWith(gone.id + ":")) s.inputs.source = "close";
    ctRenderChosen();
    ctStudiesChanged();
  });
  $("#ct-chosen").addEventListener("change", (e) => {
    const k = e.target.dataset.k, key = e.target.dataset.in;
    if (k == null) return;
    const s = ctActive().studies[Number(k)];
    s.inputs[key] = e.target.tagName === "SELECT" ? e.target.value : Number(e.target.value);
    ctRenderChosen();
    ctStudiesChanged();
  });
}

/* ---------- bar replay ---------- */

function ctReplayStart() {
  const v = CT.views[CT.active];
  if (!v?.data) return;
  if (v.data.synthetic) { toast("Bar replay works on charts drawn against time (seconds to months, not ticks, ranges, Renko, line break, Kagi or P&F)", true); return; }
  if (CT.replay && CT.replay.i !== CT.active) ctReplayExit(true);
  CT.replay = { i: CT.active, picking: true, until: null, playing: false, times: v.data.bars.map((b) => b[0]), tf: v.data.tf, seconds: v.data.seconds };
  $("#ct-replay").hidden = false;
  $("#ct-replay-at").textContent = "Click the candle to start from";
  v.el.classList.add("picking");
}

// where the chart stops when the last candle shown is the one at time x
function ctUntilFor(r, x) {
  const k = r.times.indexOf(x);
  if (r.seconds) return x + r.seconds;
  if (/W$/.test(r.tf)) return x + 7 * 86400;
  if (/M$/.test(r.tf)) return r.times[k + 1] ?? x + 31 * 86400;
  return x + 86400;
}

function ctClick(v, param) {
  const r = CT.replay;
  if (!r || !r.picking || r.i !== v.i || param.time == null) return;
  r.picking = false;
  r.at = param.time;
  r.until = ctUntilFor(r, param.time);
  v.el.classList.remove("picking");
  ctLoad(v.i, { keepView: true });
}

function ctReplayStatus() {
  const r = CT.replay;
  if (!r) return;
  const v = CT.views[r.i];
  $("#ct-replay-at").textContent = r.until ? `as of ${ctTimeText(v, r.at)} ET · nothing after it is used` : "Click the candle to start from";
  $("#ct-replay-play").textContent = r.playing ? "⏸" : "▶";
}

async function ctReplayMove(dir) {
  const r = CT.replay;
  if (!r || r.until == null) return false;
  const k = r.times.indexOf(r.at);
  const next = r.times[k + dir];
  if (next == null) { if (dir > 0) toast("Caught up with the newest candle loaded"); return false; }
  r.at = next;
  r.until = ctUntilFor(r, next);
  await ctLoad(r.i, { keepView: true, quiet: true });
  return true;
}

async function ctReplayPlay() {
  const r = CT.replay;
  if (!r || r.until == null) return;
  r.playing = !r.playing;
  ctReplayStatus();
  while (CT.replay === r && r.playing) {
    const t0 = Date.now();
    if (!(await ctReplayMove(1))) { r.playing = false; break; }
    const wait = 1000 / Number($("#ct-replay-speed").value) - (Date.now() - t0);
    if (wait > 0) await new Promise((ok) => setTimeout(ok, wait));
  }
  ctReplayStatus();
}

function ctReplayExit(reload = true) {
  const r = CT.replay;
  if (!r) return;
  r.playing = false;
  CT.replay = null;
  $("#ct-replay").hidden = true;
  CT.views[r.i]?.el.classList.remove("picking");
  if (reload) ctLoad(r.i, { keepView: false });
}

function ctWireReplay() {
  $("#ct-replay-step").addEventListener("click", () => ctReplayMove(1));
  $("#ct-replay-back").addEventListener("click", () => ctReplayMove(-1));
  $("#ct-replay-play").addEventListener("click", ctReplayPlay);
  $("#ct-replay-pick").addEventListener("click", () => {
    const r = CT.replay;
    if (!r) return;
    r.playing = false;
    r.picking = true;
    CT.views[r.i].el.classList.add("picking");
    ctReplayStatus();
    $("#ct-replay-at").textContent = "Click the candle to start from";
  });
  $("#ct-replay-exit").addEventListener("click", () => ctReplayExit(true));
}

/* ---------- watchlists ---------- */

async function ctLoadWatchlists() {
  try { CT.wl = await api("/api/watchlists"); } catch (e) { toast("Watchlists: " + e.message, true); CT.wl = { lists: [{ name: "Watchlist", symbols: ["SPY"] }], current: "Watchlist" }; }
  ctRenderWatch();
}

const ctList = () => CT.wl?.lists.find((x) => x.name === CT.wl.current) || CT.wl?.lists[0];

async function ctSaveWatchlists() {
  try { CT.wl = await api("/api/watchlists", { body: CT.wl }); } catch (e) { toast(e.message, true); await ctLoadWatchlists(); return; }
  ctRenderWatch();
}

function ctRenderWatch() {
  if (!CT.wl) return;
  $("#ct-wl").innerHTML = CT.wl.lists.map((x) => `<option ${x.name === CT.wl.current ? "selected" : ""}>${esc(x.name)}</option>`).join("");
  const list = ctList();
  const all = [...new Set(CT.wl.lists.flatMap((x) => x.symbols))];
  $("#ct-symbol-list").innerHTML = all.map((s) => `<option value="${esc(s)}">`).join("");
  $("#ct-wl-table").innerHTML = `<tr><th>Symbol</th><th class="num">Last</th><th class="num">Chg%</th><th></th></tr>` + (list?.symbols || []).map((s) => {
    const q = CT.quotes[s];
    const cls = q?.pct == null ? "" : q.pct >= 0 ? "up" : "down";
    return `<tr data-sym="${esc(s)}" class="${s === ctActive()?.symbol ? "on" : ""}"><td><b>${esc(s)}</b></td><td class="num">${q?.last == null ? "–" : num(q.last)}</td>
      <td class="num ${cls}">${q?.pct == null ? "–" : (q.pct >= 0 ? "+" : "") + num(q.pct, 2) + "%"}</td><td><button type="button" class="ghost ct-x" data-drop="${esc(s)}" title="Remove ${esc(s)}">✕</button></td></tr>`;
  }).join("");
  alFillWatchlists();
}

async function ctQuotes() {
  const list = ctList();
  if (!list || activeTab() !== "charts" || $("#ct-watch").hidden || document.hidden) return;
  try {
    const { quotes } = await api("/api/quotes", { body: { symbols: list.symbols } });
    for (const q of quotes) CT.quotes[q.symbol] = q;
    ctRenderWatch();
  } catch { /* next time */ }
}

function ctWireWatch() {
  $("#ct-wl").addEventListener("change", () => { CT.wl.current = $("#ct-wl").value; ctSaveWatchlists().then(ctQuotes); });
  $("#ct-wl-new").addEventListener("click", () => {
    const name = prompt("Name of the new watchlist")?.trim();
    if (!name) return;
    if (CT.wl.lists.some((x) => x.name === name)) { toast("There's a watchlist with that name already", true); return; }
    CT.wl.lists.push({ name, symbols: [] });
    CT.wl.current = name;
    ctSaveWatchlists();
  });
  $("#ct-wl-rename").addEventListener("click", () => {
    const list = ctList();
    const name = prompt("New name", list.name)?.trim();
    if (!name || name === list.name) return;
    list.name = name;
    CT.wl.current = name;
    ctSaveWatchlists();
  });
  $("#ct-wl-delete").addEventListener("click", () => {
    if (CT.wl.lists.length < 2) { toast("Keep at least one watchlist", true); return; }
    const list = ctList();
    if (!confirm(`Delete the watchlist "${list.name}"?`)) return;
    CT.wl.lists = CT.wl.lists.filter((x) => x !== list);
    CT.wl.current = CT.wl.lists[0].name;
    ctSaveWatchlists();
  });
  $("#ct-wl-add").addEventListener("submit", (e) => {
    e.preventDefault();
    const add = $("#ct-wl-input").value.split(/[\s,]+/).map((s) => s.trim().toUpperCase()).filter(Boolean);
    if (!add.length) return;
    const list = ctList();
    list.symbols = [...new Set([...list.symbols, ...add])];
    $("#ct-wl-input").value = "";
    ctSaveWatchlists().then(ctQuotes);
  });
  $("#ct-wl-table").addEventListener("click", (e) => {
    const drop = e.target.dataset.drop;
    if (drop) {
      const list = ctList();
      list.symbols = list.symbols.filter((s) => s !== drop);
      ctSaveWatchlists();
      return;
    }
    const sym = e.target.closest("tr[data-sym]")?.dataset.sym;
    if (sym) { ctApply({ symbol: sym, sessions: null }); ctRenderWatch(); }
  });
  $("#ct-guard-card").addEventListener("click", (e) => {
    const sym = e.target.closest("[data-sym]")?.dataset.sym;
    if (sym && sym !== CT.panels?.[CT.active]?.symbol) { ctApply({ symbol: sym, sessions: null }); ctRenderWatch(); }
  });
  $("#ct-wl-scan").addEventListener("click", async () => {
    const list = ctList();
    if (!list.symbols.length) { toast("The list is empty", true); return; }
    if (!confirm(`Make the live scanner watch ${list.symbols.join(", ")}? (It replaces the scanner's live symbols.)`)) return;
    try { await api("/api/live-symbols", { body: { action: "set", symbols: list.symbols } }); toast(`The scanner now watches "${list.name}"`); }
    catch (e) { toast(e.message, true); }
  });
  $("#ct-wl-alert").addEventListener("click", () => alPrefill({ watchlist: ctList()?.name }));
}

/* ---------- the Charts tab ---------- */

async function ctStart() {
  if (CT.started) { ctRenderGrid(); for (let i = 0; i < CT.layout; i++) if (!CT.views[i]?.data) ctLoad(i); return; }
  CT.started = true;
  ctLoadState();
  ctWireToolbar();
  ctWireStudies();
  ctWireReplay();
  ctWireWatch();
  ctdWireTools();
  ctdLoadState();
  $("#ct-watch").hidden = recall("ct-watch-hidden") === "1";
  ctRenderGrid();
  ctSyncToolbar();
  ctLoadAll();
  ctLoadWatchlists().then(ctQuotes);
  ctCatalog();
  CT.timer = setInterval(ctTick, 2500);
  CT.quoteTimer = setInterval(ctQuotes, 15000);
}

async function ctCatalog() {
  if (CT.catalog) return CT.catalog;
  try {
    CT.catalog = (await api("/api/studies")).studies;
    CT.catalogBy = Object.fromEntries(CT.catalog.map((s) => [s.type, s]));
    $("#ct-study-search").placeholder = `Search ${CT.catalog.length} indicators`;
  } catch (e) { toast("Indicators: " + e.message, true); }
  return CT.catalog;
}

loaders.charts = ctStart;

/* ---------- drawn lines, auto trend lines, break-and-retest marks, positions and the risk helper
   (paperbot/trendlines.py: the same market clock, so a line sits at the same price on every interval) ---------- */

const CTD_SESSIONS = { ext: [4 * 3600, 20 * 3600], rth: [9.5 * 3600, 16 * 3600], "24h": [0, 24 * 3600] };
const CTD_EPOCH_DAY = Date.UTC(1970, 0, 5) / 86400000; // a Monday
const ctdOffsets = new Map(); // hour -> the ET offset from UTC, in seconds
const ctdEtFmt = new Intl.DateTimeFormat("en-US", { timeZone: ET, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit" });
Object.assign(CT, { tool: "cursor", sel: null, draft: null, drag: null, lines: {}, saveTimers: {}, state: null });

function ctdOffset(t) {
  const k = Math.floor(t / 3600);
  let off = ctdOffsets.get(k);
  if (off == null) {
    const p = Object.fromEntries(ctdEtFmt.formatToParts(new Date(k * 3600000)).map((x) => [x.type, x.value]));
    off = Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour % 24, +p.minute, +p.second) / 1000 - k * 3600;
    ctdOffsets.set(k, off);
  }
  return off;
}

// seconds of session time from a fixed Monday to t: nights, weekends and market holidays add nothing
// (trendlines.py market_clock; the holidays come with each chart)
function ctdClock(t, mode = "ext") {
  let raw = ctdRaw(t + ctdOffset(t), mode);
  const starts = ctdHolidayStarts(mode), len = mode === "24h" ? 86400 : CTD_SESSIONS[mode][1] - CTD_SESSIONS[mode][0];
  let lo = 0, hi = starts.length;
  while (lo < hi) { const mid = (lo + hi) >> 1; if (starts[mid] <= raw) lo = mid + 1; else hi = mid; }
  if (lo) raw -= (lo - 1) * len + Math.min(raw - starts[lo - 1], len);
  return raw;
}

function ctdHolidayStarts(mode) {
  CT.holidayStarts ||= {};
  if (!CT.holidayStarts[mode]) {
    CT.holidayStarts[mode] = (CT.holidays || []).map((iso) => {
      const [y, m, d] = iso.split("-").map(Number);
      // a holiday's window starts at midnight; for the 24-hour market, at 20:00 the evening before
      return ctdRaw(mode === "24h" ? Date.UTC(y, m - 1, d - 1, 20) / 1000 : Date.UTC(y, m - 1, d) / 1000, mode);
    }).sort((a, b) => a - b);
  }
  return CT.holidayStarts[mode];
}

// the clock from a wall-clock time (ET, in seconds as if it were UTC), before holidays
function ctdRaw(wall, mode) {
  if (mode === "24h") { // the 24-hour market: Sunday 20:00 to Friday 20:00 runs on, the weekend adds nothing
    const day = Math.floor(wall / 86400);
    const days = day - (CTD_EPOCH_DAY - 1); // from a Sunday
    const into = (((days % 7) + 7) % 7) * 86400 + (wall - day * 86400);
    return Math.floor(days / 7) * 432000 + Math.min(Math.max(into - 72000, 0), 432000);
  }
  const [lo, hi] = CTD_SESSIONS[mode];
  const len = hi - lo;
  const day = Math.floor(wall / 86400);
  const days = day - CTD_EPOCH_DAY;
  const dow = ((days % 7) + 7) % 7; // 0 is Monday
  const weekdays = Math.floor(days / 7) * 5 + Math.min(dow, 5);
  if (dow >= 5) return weekdays * len;
  return weekdays * len + Math.min(Math.max(wall - day * 86400 - lo, 0), len);
}

function ctdClocks(v, mode) {
  v.clk ||= {};
  if (!v.clk[mode]) v.clk[mode] = v.data.bars.map((b) => ctdClock(b[7], mode));
  return v.clk[mode];
}

// how far the clock moves per candle on this chart: the average over its last 50 candles (a regular-hours 4h chart
// moves a 24-hour line on by about 12 hours a candle, not 4), for drawing past either end
function ctdStep(v, mode) {
  v.steps ||= {};
  if (v.steps[mode]) return v.steps[mode];
  const clk = ctdClocks(v, mode), n = clk.length, k = Math.min(n - 1, 50);
  const avg = k > 0 ? (clk[n - 1] - clk[n - 1 - k]) / k : 0;
  const d = v.data, len = CTD_SESSIONS[mode][1] - CTD_SESSIONS[mode][0];
  v.steps[mode] = avg > 0 ? avg : d.seconds || (/W$/.test(d.tf) ? 5 * len : /M$/.test(d.tf) ? 21 * len : len);
  return v.steps[mode];
}

// the x of a candle position that may fall between candles (the chart library places whole candles only)
function ctdX(ts, k) {
  const i = Math.floor(k), f = k - i;
  const a = ts.logicalToCoordinate(i);
  if (a == null || f < 1e-9) return a;
  const b = ts.logicalToCoordinate(i + 1);
  return b == null ? a : a + f * (b - a);
}

// where a moment (in session time) falls on the chart, in candles: fractional, and past either end at the chart's pace
function ctdLogical(v, c, mode) {
  const clk = ctdClocks(v, mode), n = clk.length;
  if (!n) return null;
  const step = ctdStep(v, mode);
  if (c <= clk[0]) return (c - clk[0]) / step;
  if (c >= clk[n - 1]) return n - 1 + (c - clk[n - 1]) / step;
  let lo = 0, hi = n - 1;
  while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (clk[mid] <= c) lo = mid; else hi = mid; }
  return clk[hi] === clk[lo] ? lo : lo + (c - clk[lo]) / (clk[hi] - clk[lo]);
}

function ctdClockAt(v, k, mode) {
  const clk = ctdClocks(v, mode), n = clk.length, step = ctdStep(v, mode);
  if (k <= 0) return clk[0] + k * step;
  if (k >= n - 1) return clk[n - 1] + (k - n + 1) * step;
  const i = Math.floor(k);
  return clk[i] + (k - i) * (clk[i + 1] - clk[i]);
}

function ctdPriceAt(line, c) {
  if (line.kind !== "trend") return line.p1;
  const c1 = ctdClock(line.t1, line.clock || "ext"), c2 = ctdClock(line.t2, line.clock || "ext");
  return c2 === c1 ? line.p1 : line.p1 + ((line.p2 - line.p1) * (c - c1)) / (c2 - c1);
}

function ctdLabel(line) {
  if (line.name) return line.name;
  return line.kind === "trend" ? `trend line ${num(line.p1)} to ${num(line.p2)}` : `${line.kind === "hline" ? "horizontal line" : "horizontal ray"} ${num(line.p1)}`;
}

// a line's points on screen, across the visible candles; `end` (a candle) stops it there
function ctdPath(v, line, end = null) {
  const ts = v.chart.timeScale(), r = ts.getVisibleLogicalRange();
  if (!r || !v.data?.bars.length) return [];
  const mode = line.clock || "ext";
  const left = r.from - 2, right = r.to + 2;
  let a = left, z = right;
  if (line.kind !== "hline") a = ctdLogical(v, ctdClock(line.t1, mode), mode);
  if (line.kind === "trend" && !line.extend) z = ctdLogical(v, ctdClock(line.t2, mode), mode);
  if (end != null) z = Math.min(z, end);
  const from = Math.max(a, left), to = Math.min(z, right);
  if (a == null || !(to >= from)) return [];
  const pts = [];
  const at = (k) => {
    const x = ctdX(ts, k);
    const y = v.series.priceToCoordinate(line.kind === "trend" ? ctdPriceAt(line, ctdClockAt(v, k, mode)) : line.p1);
    if (x != null && y != null) pts.push([x, y]);
  };
  at(from);
  if (line.kind === "trend") for (let k = Math.ceil(from); k < to; k++) at(k);
  at(to);
  return pts;
}

function ctdAnchor(v, line, which) {
  const mode = line.clock || "ext";
  const t = which === 1 ? line.t1 : line.t2, p = which === 1 ? line.p1 : line.p2;
  const k = ctdLogical(v, ctdClock(t, mode), mode);
  const x = k == null ? null : ctdX(v.chart.timeScale(), k), y = v.series.priceToCoordinate(p);
  return x == null || y == null ? null : [x, y];
}

const ctdLines = (sym) => CT.lines[sym] || [];

// seconds in an interval label: 5m, 1h, 30s, 1D, 1W, 1M (0 for ticks and ranges)
function ctdSeconds(tf) {
  const m = /^(\d+)\s*([smhDWM])$/.exec(String(tf || ""));
  if (!m) return 0;
  return Number(m[1]) * { s: 1, m: 60, h: 3600, D: 86400, W: 604800, M: 2592000 }[m[2]];
}

// a trend line drawn on a short interval isn't shown on one more than 15 times longer (a 5-minute line across a 4-hour
// chart says nothing); horizontal lines show everywhere
function ctdHidden(v, line) {
  if (line.kind !== "trend" || !line.tf) return false;
  const here = v.data.seconds || ctdSeconds(v.data.tf), drawn = ctdSeconds(line.tf);
  return here > 0 && drawn > 0 && here > 15 * drawn; // a 1-minute line still shows on 5m and 15m
}

// a trend line from another session's chart: on this one's candles it isn't straight (nights or extended hours it
// counts aren't here, or the other way round), so it's dashed
function ctdOtherSession(v, line) {
  const d = v.data;
  if (line.kind !== "trend" || !d.intraday) return false;
  const here = d.session === "24h" ? "24h" : v.panel.ext ? "ext" : "rth";
  return (line.clock || "ext") !== here;
}

const CTD_SESSION_NAMES = { "24h": "24 hours", ext: "Extended 4:00–20:00", rth: "Regular hours" };

function ctdLineNotes(v) {
  const lines = ctdLines(v.panel.symbol).filter((l) => l.kind === "trend");
  const hidden = lines.filter((l) => ctdHidden(v, l)).length;
  const other = [...new Set(lines.filter((l) => !ctdHidden(v, l) && ctdOtherSession(v, l)).map((l) => l.clock || "ext"))];
  const out = [];
  if (hidden) out.push(`${hidden} trend line${hidden === 1 ? "" : "s"} drawn on a much shorter interval ${hidden === 1 ? "isn't" : "aren't"} shown here`);
  if (other.length) out.push(`Dashed lines were drawn on ${other.map((c) => `"${CTD_SESSION_NAMES[c]}"`).join(" or ")}: pick that session to see them straight`);
  return out;
}
const ctdTicker = (sym) => /^[A-Z][A-Z0-9.]{0,9}$/.test(sym);

class CtDrawings {
  constructor(v) {
    this.v = v;
    this.views = [{ zOrder: () => "top", renderer: () => ({ draw: (t) => this.draw(t) }) }];
  }
  attached({ chart, series, requestUpdate }) { this.chart = chart; this.series = series; this.requestUpdate = requestUpdate; }
  detached() { this.chart = this.series = this.requestUpdate = null; }
  updateAllViews() {}
  paneViews() { return this.views; }
  update() { this.requestUpdate?.(); }
  draw(target) {
    const v = this.v, d = v.data, p = v.panel;
    if (!this.chart || !d || d.synthetic || !d.bars.length) return;
    target.useMediaCoordinateSpace(({ context: ctx }) => {
      const stroke = (pts, color, width, dash) => {
        if (pts.length < 2) return;
        ctx.strokeStyle = color; ctx.lineWidth = width; ctx.setLineDash(dash || []);
        ctx.beginPath(); ctx.moveTo(pts[0][0], pts[0][1]);
        for (const q of pts.slice(1)) ctx.lineTo(q[0], q[1]);
        ctx.stroke(); ctx.setLineDash([]);
      };
      ctx.font = "600 10px -apple-system, BlinkMacSystemFont, sans-serif";
      ctx.textBaseline = "bottom";
      if (p.ov.auto) {
        for (const a of d.auto_lines || []) {
          const line = { kind: "trend", t1: a.t1, p1: a.p1, t2: a.t2, p2: a.p2, clock: a.clock, extend: true };
          const color = a.side === "resistance" ? "#f77c80" : "#4fc0a8";
          const pts = ctdPath(v, line, a.broken ? v.index.get(a.end) : null);
          stroke(pts, color, 1.5, [6, 4]);
          const tip = ctdAnchor(v, line, 2);
          if (tip) { ctx.fillStyle = color; ctx.textAlign = "center"; ctx.fillText(`${a.touches} touches`, tip[0], tip[1] - (a.side === "resistance" ? 6 : -16)); }
        }
      }
      v.paths = new Map();
      for (const line of ctdLines(p.symbol)) {
        if (ctdHidden(v, line)) continue;
        const pts = ctdPath(v, line);
        v.paths.set(line.id, pts);
        const on = CT.sel?.sym === p.symbol && CT.sel.id === line.id;
        stroke(pts, line.color || "#2962ff", on ? 2.5 : 1.5, ctdOtherSession(v, line) ? [6, 4] : null);
        if (on) {
          for (const w of line.kind === "trend" ? [1, 2] : [1]) {
            const q = ctdAnchor(v, line, w);
            if (!q) continue;
            ctx.fillStyle = "#0f0f0f"; ctx.strokeStyle = line.color || "#2962ff"; ctx.lineWidth = 2;
            ctx.beginPath(); ctx.arc(q[0], q[1], 5, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
          }
        }
        if (line.name && pts.length) { // at the line's right end, as TradingView labels a line
          const [x, y] = pts.at(-1);
          ctx.fillStyle = line.color || "#2962ff"; ctx.textAlign = "right";
          ctx.fillText(line.name, Math.max(x - 6, ctx.measureText(line.name).width + 4), y - 4);
        }
      }
      const dr = CT.draft;
      if (dr && dr.v === v && dr.t2 != null) {
        const line = { kind: "trend", t1: dr.t1, p1: dr.p1, t2: dr.t2, p2: dr.p2, clock: dr.clock, extend: false };
        stroke(ctdPath(v, line), CT_DRAW_COLOR(), 1.5, [4, 3]);
      }
    });
  }
}

const CT_DRAW_COLOR = () => $("#ct-draw-color")?.value || "#2962ff";

// the candle under the pointer, and the price (snapped to its open, high, low or close with the magnet)
function ctdPoint(v, x, y) {
  const ts = v.chart.timeScale(), bars = v.data.bars;
  const l = ts.coordinateToLogical(x);
  if (l == null || !bars.length) return null;
  const k = Math.max(0, Math.min(bars.length - 1, Math.round(l)));
  const b = bars[k];
  let price = v.series.coordinateToPrice(y);
  if (price == null) return null;
  if ($("#ct-magnet").checked) price = [b[1], b[2], b[3], b[4]].reduce((best, q) => (Math.abs(q - price) < Math.abs(best - price) ? q : best), b[4]);
  return { k, t: b[7], p: Math.round(price * 1e6) / 1e6 };
}

function ctdDistance(px, py, pts) {
  let best = Infinity;
  for (let i = 1; i < pts.length; i++) {
    const [x1, y1] = pts[i - 1], [x2, y2] = pts[i];
    const dx = x2 - x1, dy = y2 - y1, len = dx * dx + dy * dy;
    const t = len ? Math.max(0, Math.min(1, ((px - x1) * dx + (py - y1) * dy) / len)) : 0;
    best = Math.min(best, Math.hypot(px - (x1 + t * dx), py - (y1 + t * dy)));
  }
  return best;
}

function ctdHit(v, x, y) {
  const lines = ctdLines(v.panel.symbol);
  for (const line of lines.slice().reverse()) {
    if (CT.sel?.id === line.id) {
      for (const w of line.kind === "trend" ? [1, 2] : []) {
        const q = ctdAnchor(v, line, w);
        if (q && Math.hypot(q[0] - x, q[1] - y) <= 8) return { line, part: w === 1 ? "p1" : "p2" };
      }
    }
    if (ctdDistance(x, y, v.paths?.get(line.id) || []) <= 6) return { line, part: "body" };
  }
  return null;
}

function ctdRepaint(sym) {
  for (const v of CT.views) if (v?.draw && v.panel.symbol === sym) v.draw.update();
}

function ctdSelect(sel) {
  CT.sel = sel;
  $("#ct-draw-delete").disabled = $("#ct-line-alert").disabled = !sel;
  const line = sel && ctdLines(sel.sym).find((x) => x.id === sel.id);
  if (line) $("#ct-draw-color").value = line.color || "#2962ff";
  for (const v of CT.views) v?.draw?.update();
}

function ctdTool(tool) {
  CT.tool = tool;
  CT.draft = null;
  $$("#ct-tools button").forEach((b) => b.classList.toggle("on", b.dataset.tool === tool));
  for (const v of CT.views) if (v?.main) v.main.classList.toggle("drawing", tool !== "cursor");
}

async function ctdFetch(sym) {
  if (!ctdTicker(sym)) return;
  // again every 10 seconds, so lines drawn in another tab (or by paperbot) show up; never mid-drag or before a save lands
  const fresh = CT.linesAt?.[sym] && Date.now() - CT.linesAt[sym] < 10000;
  if ((CT.lines[sym] !== undefined && fresh) || CT.drag || CT.draft || CT.savePending?.[sym]) return;
  CT.linesAt ||= {};
  CT.linesAt[sym] = Date.now();
  if (CT.lines[sym] === undefined) CT.lines[sym] = [];
  try {
    const got = (await api(`/api/drawings?symbol=${encodeURIComponent(sym)}`)).drawings;
    if (CT.drag || CT.draft || CT.savePending?.[sym]) return;
    CT.lines[sym] = got;
    ctdSeenAuto(sym, got);
  } catch { /* keep what's shown */ }
  ctdRepaint(sym);
  for (const v of CT.views) if (v?.data && v.panel.symbol === sym) { ctPaintOverlays(v); ctLegend(v); } // their break and retest marks
}

// the 1-minute line keeper's lines (paperbot/linekeeper.py) as this page last loaded them: sent with each save, so the
// server can tell a keeper line you moved or deleted from one this page just hadn't caught up with yet
function ctdSeenAuto(sym, lines) {
  CT.autoSeen ||= {};
  CT.autoSeen[sym] = Object.fromEntries((lines || []).filter((d) => String(d.id).startsWith("auto1m-")).map((d) => [d.id, { ...d }]));
}

function ctdSave(sym) {
  clearTimeout(CT.saveTimers[sym]);
  const timer = setTimeout(async () => {
    try {
      CT.lines[sym] = (await api("/api/drawings", { body: { symbol: sym, drawings: ctdLines(sym), auto_seen: CT.autoSeen?.[sym] || {} } })).drawings;
      ctdSeenAuto(sym, CT.lines[sym]);
    } catch (e) { toast("Couldn't save the lines: " + e.message, true); }
    ctdRepaint(sym);
    CT.savePending[sym] = false;
    CT.linesAt ||= {};
    CT.linesAt[sym] = Date.now();
    for (let i = 0; i < CT.layout; i++) if (CT.views[i]?.panel.symbol === sym) ctLoad(i, { keepView: true, quiet: true }); // new break and retest marks
  }, 400);
  CT.savePending ||= {};
  CT.savePending[sym] = true;
  CT.saveTimers[sym] = timer;
}

function ctdNew(v, kind, a, b) {
  const p = v.panel, d = v.data;
  const id = Math.random().toString(16).slice(2, 10);
  const clock = d.session === "24h" ? "24h" : d.intraday && !p.ext ? "rth" : "ext";
  const line = { id, kind, t1: a.t, p1: a.p, t2: (b || a).t, p2: (b || a).p, clock, extend: true, name: "", color: CT_DRAW_COLOR(), tf: d.tf };
  if (kind === "trend" && line.t2 < line.t1) Object.assign(line, { t1: line.t2, p1: line.p2, t2: a.t, p2: a.p });
  CT.lines[p.symbol] = [...ctdLines(p.symbol), line];
  ctdSave(p.symbol);
  ctdTool("cursor");
  ctdSelect({ sym: p.symbol, id });
}

function ctdDown(v, e) {
  if (e.button !== 0 || !v.data || !v.chart) return;
  const r = v.main.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
  if (x > r.width - 72) return; // the price scale
  const sym = v.panel.symbol;
  if (CT.tool !== "cursor") {
    e.preventDefault(); e.stopPropagation();
    if (v.data.synthetic) { toast("Lines are drawn on charts against time (not ticks, ranges, Renko, line break, Kagi or P&F)", true); return; }
    if (!ctdTicker(sym)) { toast("Lines are kept per ticker, not on a formula", true); return; }
    const pt = ctdPoint(v, x, y);
    if (!pt) return;
    if (CT.tool === "hline" || CT.tool === "hray") return ctdNew(v, CT.tool, pt);
    if (!CT.draft || CT.draft.v !== v) { CT.draft = { v, t1: pt.t, p1: pt.p, clock: v.data.session === "24h" ? "24h" : v.data.intraday && !v.panel.ext ? "rth" : "ext" }; return; }
    if (pt.t === CT.draft.t1) return;
    const first = { t: CT.draft.t1, p: CT.draft.p1 };
    CT.draft = null;
    return ctdNew(v, "trend", first, pt);
  }
  const hit = ctdHit(v, x, y);
  if (!hit) { if (CT.sel) ctdSelect(null); return; }
  e.preventDefault(); e.stopPropagation();
  ctdSelect({ sym, id: hit.line.id });
  const start = ctdPoint(v, x, y);
  const mode = hit.line.clock || "ext";
  CT.drag = { v, id: hit.line.id, part: hit.part, start, raw: v.series.coordinateToPrice(y), orig: { ...hit.line },
    k1: Math.round(ctdLogical(v, ctdClock(hit.line.t1, mode), mode)), k2: Math.round(ctdLogical(v, ctdClock(hit.line.t2, mode), mode)) };
  v.chart.applyOptions({ handleScroll: false, handleScale: false });
}

function ctdMove(e) {
  const g = CT.drag, dr = CT.draft;
  const v = g?.v || dr?.v;
  if (!v?.chart) return;
  const r = v.main.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
  const pt = ctdPoint(v, x, y);
  if (!pt) return;
  if (dr && !g) { dr.t2 = pt.t; dr.p2 = pt.p; v.draw.update(); return; }
  const line = ctdLines(v.panel.symbol).find((l) => l.id === g.id);
  if (!line) return;
  const bars = v.data.bars, n = bars.length;
  const timeAt = (k) => bars[Math.max(0, Math.min(n - 1, k))][7];
  if (g.part === "p1") Object.assign(line, { t1: pt.t, p1: pt.p });
  else if (g.part === "p2") Object.assign(line, { t2: pt.t, p2: pt.p });
  else {
    const dk = pt.k - g.start.k;
    const dp = v.series.coordinateToPrice(y) - g.raw;
    Object.assign(line, { p1: g.orig.p1 + dp, p2: g.orig.p2 + dp });
    if (line.kind !== "hline") Object.assign(line, { t1: timeAt(g.k1 + dk), t2: line.kind === "trend" ? timeAt(g.k2 + dk) : timeAt(g.k1 + dk) });
  }
  g.moved = true;
  ctdRepaint(v.panel.symbol);
}

function ctdUp() {
  const g = CT.drag;
  if (!g) return;
  CT.drag = null;
  g.v.chart?.applyOptions({ handleScroll: true, handleScale: true });
  const line = ctdLines(g.v.panel.symbol).find((l) => l.id === g.id);
  if (line && line.kind === "trend" && line.t2 < line.t1) Object.assign(line, { t1: line.t2, p1: line.p2, t2: line.t1, p2: line.p1 });
  if (g.moved) ctdSave(g.v.panel.symbol);
}

function ctdWire(v) {
  if (v.wired) return;
  v.wired = true;
  v.main.addEventListener("mousedown", (e) => ctdDown(v, e), true);
  v.main.addEventListener("mousemove", (e) => {
    if (CT.tool !== "cursor" || CT.drag || !v.data || !v.chart) return;
    const r = v.main.getBoundingClientRect();
    v.main.style.cursor = ctdHit(v, e.clientX - r.left, e.clientY - r.top) ? "pointer" : "";
  });
}

function ctdDelete() {
  const s = CT.sel;
  if (!s) return;
  CT.lines[s.sym] = ctdLines(s.sym).filter((l) => l.id !== s.id);
  ctdSelect(null);
  ctdSave(s.sym);
}

// each drawn line's break, retest and outcome, as labels on their candles (the text is the server's, from paperbot/trendlines.py)
function ctdStageLabels(v, byTime) {
  const d = v.data, p = v.panel;
  if (!p.ov.stages || !d.line_stages) return [];
  const names = new Map(ctdLines(p.symbol).map((l) => [l.id, l]));
  const out = [];
  for (const [id, stages] of Object.entries(d.line_stages)) {
    const line = names.get(id);
    if (!line) continue;
    for (const s of stages) {
      if (!byTime.has(s.x)) continue;
      const up = s.side === "up";
      const name = s.stage === "broken" ? `Break ${up ? "↑" : "↓"}` : s.stage === "confirmed" ? "Retest held" : "Retest failed";
      const color = s.stage === "broken" ? "#ff9800" : s.stage === "confirmed" ? (up ? "#089981" : "#f23645") : "#787b86";
      out.push({ t: s.x, side: up ? "long" : "short", name, color, text: s.text, source: "line" });
    }
  }
  return out;
}

// a crossing alert on every line of this chart's symbol that doesn't have one yet (1-minute candles, to the phone)
async function ctdAlertAll() {
  const p = ctActive(), sym = p.symbol;
  const lines = ctdLines(sym);
  if (!ctdTicker(sym) || !lines.length) { toast("No lines on this symbol yet: draw some first", true); return; }
  let have;
  try { have = new Set((await api("/api/alerts")).alerts.map((a) => a.name)); } catch (e) { toast(e.message, true); return; }
  let made = 0;
  for (const line of lines) {
    const name = `${sym} crosses ${ctdLabel(line)}`;
    if (have.has(name)) continue;
    try {
      await api("/api/alerts", { body: { name, symbols: [sym], tf: "1m", trigger: "bar", phone: true,
        session: p.session === "24h" || line.clock === "24h" ? "24h" : "ext",
        conditions: [{ left: { kind: "price" }, op: "crosses", right: { kind: "line", symbol: sym, line } }] } });
      made++;
    } catch (e) { toast(`${ctdLabel(line)}: ${e.message}`, true); }
  }
  toast(made ? `${made} crossing alert${made === 1 ? "" : "s"} set: your phone gets one when price crosses a line` : "Every line already has a crossing alert");
  if (activeTab() === "alerts") alLoad();
}

/* ---------- the position guard (paperbot/guard.py): your positions against the lines, on the chart ---------- */

const CTG_LOOK = {
  exit: { name: "EXIT", color: "#f23645", side: "short", cls: "exit" },
  against: { name: "Against you", color: "#ff9800", side: "short", cls: "against" },
  failed: { name: "Failed break", color: "#787b86", side: "short", cls: "failed" },
  target: { name: "Target", color: "#089981", side: "long", cls: "target" },
  with: { name: "With you", color: "#089981", side: "long", cls: "with" },
  hold: { name: "Hold", color: "#2962ff", side: "long", cls: "hold" },
};
// the banner's colour and badge for the rule's verdict on each position (paperbot/guard.py verdict())
const CTG_VERDICT = { hold: ["HOLD", "hold"], exit: ["EXIT", "exit"], no_floor: ["NO FLOOR", ""], none: ["…", ""] };

// the guard's events for this chart's symbol, as labels on the candle each happened on
function ctgLabels(v) {
  const g = CT.guard, d = v.data, sym = v.panel.symbol;
  if (!g || !d || d.synthetic || !d.bars.length) return [];
  const out = [];
  for (const e of g.events || []) {
    const look = CTG_LOOK[e.kind];
    if (!look || e.symbol !== sym) continue;
    let k = -1; // the candle that holds the event's 1-minute candle
    for (let i = d.bars.length - 1; i >= 0; i--) if (d.bars[i][7] <= e.t) { k = i; break; }
    if (k < 0 || (d.seconds && e.t >= d.bars[k][7] + d.seconds)) continue;
    out.push({ t: d.bars[k][0], side: look.side, name: look.name, color: look.color, text: e.text, source: "guard" });
  }
  return out;
}

// a line in the banner: its name, its price unless the name already says it, and how far price is from it
function ctgLine(x) {
  const price = Number(x.price).toFixed(2);
  return `<b>${esc(x.name)}</b>${String(x.name).includes(price) ? "" : " " + price} (${num(x.distance)} away)`;
}

// your positions, in the watchlist panel beside the charts (out of the candles' way): the rule's verdict on each
// (HOLD / EXIT / no floor), the line under the trade (over it, for puts), the next level, an option's close on its last
// day, the latest warning and when the last HOLD check-in went out. Click one to chart its symbol.
function ctgCard() {
  const el = $("#ct-guard-card");
  if (!el) return;
  const g = CT.guard, mine = g?.positions || [];
  if (!mine.length) { el.hidden = true; return; }
  el.hidden = false;
  const now = Date.now() / 1000;
  const recent = (g.events || []).find((e) => CTG_LOOK[e.kind] && e.kind !== "hold" && now - e.t < 900);
  const lastHold = (g.events || []).find((e) => e.kind === "hold");
  el.innerHTML = `<div class="ct-guard-head"><b>Your positions</b>` +
      (g.updated ? `<span class="muted">checked ${esc(etTime(new Date(g.updated)))} ET</span>` : "") + `</div>` +
    mine.map((p) => {
      if (p.error) return `<div class="ct-gpos" data-sym="${esc(p.symbol)}"><b>${esc(p.position)}</b><div class="err-text">${esc(p.error)}</div></div>`;
      const v = p.verdict, look = v ? (CTG_VERDICT[v.kind] || [v.kind, ""]) : null;
      const tone = !v ? "" : v.kind === "hold" && v.near ? "near" : look[1];
      return `<div class="ct-gpos ${tone}" data-sym="${esc(p.symbol)}" title="${esc(v?.text || "")}">` +
        `<div>${look ? `<span class="ct-verdict ${look[1]}${v.near ? " near" : ""}">${esc(look[0])}</span> ` : ""}<b>${esc(p.position)}</b>` +
        (p.pnl != null ? ` <span class="${p.pnl >= 0 ? "up" : "down"}">${money(p.pnl)}</span>` : "") + `</div>` +
        (p.against ? `<div>${p.bullish === false ? "Ceiling" : "Floor"}: ${ctgLine(p.against)}</div>`
          : `<div>No line ${p.bullish === false ? "over" : "under"} the trade</div>`) +
        (p.ahead ? `<div>Target: ${ctgLine(p.ahead)}</div>` : "") +
        (p.expires_today ? `<div><b>Expires today</b>${p.close ? `: market closes ${esc(p.close)} ET` : ""}</div>` : "") + `</div>`;
    }).join("") +
    (recent ? `<div class="ct-guard-msg ${CTG_LOOK[recent.kind].cls}"><b>${esc(CTG_LOOK[recent.kind].name)}</b>: ${esc(recent.text)}</div>` : "") +
    (lastHold ? `<div class="muted" title="${esc(lastHold.text)}">Last HOLD check-in ${esc(etTime(new Date(lastHold.at)))} ET (hover to read it); one every 15 minutes, to your phone too</div>` : "");
}

async function ctgPoll() {
  if (document.hidden) return;
  let g;
  try { g = await api("/api/guard"); } catch { return; }
  const before = new Set((CT.guard?.events || []).map((e) => `${e.at}|${e.line}|${e.kind}`));
  const fresh = CT.guard ? (g.events || []).filter((e) => !before.has(`${e.at}|${e.line}|${e.kind}`) && CTG_LOOK[e.kind]) : [];
  CT.guard = g;
  for (const e of fresh.slice(0, 3)) {
    toast(`${CTG_LOOK[e.kind].name}: ${e.text}`, e.kind === "exit" || e.kind === "against");
    if ("Notification" in window && Notification.permission === "granted") new Notification(`${CTG_LOOK[e.kind].name}: ${e.symbol}`, { body: e.text });
  }
  if (activeTab() !== "charts") return;
  ctgCard();
  for (let i = 0; i < CT.layout; i++) {
    const v = CT.views[i];
    if (!v?.data) continue;
    ctPaintOverlays(v);
  }
}

/* ---------- your Robinhood positions on the chart (from the scanner's state) ---------- */

async function ctdLoadState() {
  if (activeTab() !== "charts" || document.hidden) return;
  try { CT.state = await api("/api/state"); } catch { return; }
  for (let i = 0; i < CT.layout; i++) if (CT.views[i]?.series) ctdPositions(CT.views[i]);
}

function ctdPositions(v) {
  for (const l of v.posLines || []) { try { v.series.removePriceLine(l); } catch { /* the series was rebuilt */ } }
  v.posLines = [];
  const p = v.panel, st = CT.state;
  if (!p.ov.positions || !st || !v.series || v.data?.synthetic) return;
  for (const h of (st.holdings || []).filter((x) => x.symbol === p.symbol)) {
    v.posLines.push(v.series.createPriceLine({ price: h.avg, color: "#fbc02d", lineWidth: 1, lineStyle: 2, axisLabelVisible: true,
      title: `${h.qty > 0 ? "Long" : "Short"} ${Math.abs(h.qty)} @ ${num(h.avg)} · ${money(h.pnl)}` }));
  }
  for (const o of (st.options || []).filter((x) => x.symbol === p.symbol)) {
    v.posLines.push(v.series.createPriceLine({ price: Number(o.strike), color: "#ce93d8", lineWidth: 1, lineStyle: 2, axisLabelVisible: true,
      title: `${Math.abs(o.qty)}× ${o.strike} ${o.right} ${String(o.expiration).slice(5)} · mark ${num(o.mark)} · ${money(o.pnl)}` }));
  }
}

/* ---------- the risk helper: shares or contracts from account size, risk and stop (arithmetic only) ---------- */

// the nearest line (drawn, or found) on the stop's side of price at the last candle, and a buffer past it
function ctdDefaultStop(v, side, entry) {
  const d = v.data, bars = d.bars, n = bars.length;
  if (!n || d.synthetic) return null;
  const lines = [...ctdLines(v.panel.symbol).map((l) => ({ line: l, name: `the ${ctdLabel(l)}` })),
    ...(v.panel.ov.auto ? (d.auto_lines || []).filter((a) => !a.broken).map((a) => ({
      line: { kind: "trend", t1: a.t1, p1: a.p1, t2: a.t2, p2: a.p2, clock: a.clock }, name: `the ${a.side} line through ${a.touches} swing points` })) : [])];
  const last = bars[n - 1][7];
  let best = null;
  for (const x of lines) {
    if (x.line.kind !== "hline" && last < x.line.t1) continue;
    const price = ctdPriceAt(x.line, ctdClock(last, x.line.clock || "ext"));
    if (side === "buy" ? price < entry && (!best || price > best.price) : price > entry && (!best || price < best.price)) best = { ...x, price };
  }
  if (!best) return null;
  const recent = bars.slice(-14);
  const buffer = Math.max(0.01, Math.round((recent.reduce((s, b) => s + (b[2] - b[3]), 0) / recent.length / 10) * 100) / 100);
  return { stop: side === "buy" ? best.price - buffer : best.price + buffer, line: best, buffer };
}

function ctdRiskOpen() {
  const v = CT.views[CT.active];
  const last = v?.data?.bars?.at(-1)?.[4];
  if (last != null) $("#rk-entry").value = Number(last).toFixed(2);
  $("#rk-account").value = recall("risk-account") || "";
  $("#rk-pct").value = recall("risk-pct") || "1";
  ctdRiskStop();
  ctdRiskCompute();
  $("#ct-risk").showModal();
}

function ctdRiskStop() {
  const v = CT.views[CT.active];
  const side = $("#rk-side").value, entry = Number($("#rk-entry").value);
  const s = v?.data && entry ? ctdDefaultStop(v, side, entry) : null;
  if (s) {
    $("#rk-stop").value = s.stop.toFixed(2);
    $("#rk-stop-why").textContent = `Stop: ${num(s.buffer)} ${side === "buy" ? "below" : "above"} ${s.line.name} (${num(s.line.price)} at the last candle); ` +
      `${num(s.buffer)} is a tenth of the last 14 candles' average range.`;
  } else {
    $("#rk-stop-why").textContent = `No line ${side === "buy" ? "below" : "above"} price on this chart: draw the opposing line, or type a stop.`;
  }
}

function ctdRiskCompute() {
  const n = (id) => Number($(id).value);
  const account = n("#rk-account"), pctRisk = n("#rk-pct"), entry = n("#rk-entry"), stop = n("#rk-stop");
  const prem = n("#rk-prem"), premStop = n("#rk-prem-stop"), side = $("#rk-side").value;
  remember("risk-account", $("#rk-account").value);
  remember("risk-pct", $("#rk-pct").value);
  const lines = [];
  if (!(account > 0) || !(pctRisk > 0)) { $("#rk-out").innerHTML = `<div class="muted">Enter your account size and the risk %.</div>`; return; }
  const risk = (account * pctRisk) / 100;
  lines.push(`Risk: <b>$${risk.toFixed(2)}</b> (${pctRisk}% of $${account.toLocaleString("en-US")})`);
  if (entry > 0 && stop > 0) {
    const per = Math.abs(entry - stop);
    if ((side === "buy" && stop >= entry) || (side === "sell" && stop <= entry)) lines.push(`<span class="err-text">The stop is on the wrong side of the entry for a ${side}.</span>`);
    else if (per > 0) {
      const shares = Math.floor(risk / per);
      lines.push(`Shares: <b>${shares}</b> ($${per.toFixed(2)} a share to the stop; ${shares} × ${entry.toFixed(2)} = $${(shares * entry).toLocaleString("en-US", { maximumFractionDigits: 0 })})`);
      if (shares * entry > account) lines.push(`<span class="err-text">That's more than the account: it would need margin.</span>`);
    }
  }
  if (prem > 0 && premStop >= 0 && prem !== premStop) {
    const per = Math.abs(prem - premStop) * 100;
    const contracts = Math.floor(risk / per);
    lines.push(`Contracts: <b>${contracts}</b> ($${per.toFixed(0)} a contract from ${prem.toFixed(2)} to ${premStop.toFixed(2)}; cost $${(contracts * prem * 100).toLocaleString("en-US", { maximumFractionDigits: 0 })})`);
  }
  lines.push(`<span class="muted small">Arithmetic only, not advice. paperbot never places orders on Robinhood.</span>`);
  $("#rk-out").innerHTML = lines.map((x) => `<div>${x}</div>`).join("");
}

function ctdWireTools() {
  $("#ct-tools").addEventListener("click", (e) => { const t = e.target.closest("[data-tool]")?.dataset.tool; if (t) ctdTool(t); });
  window.addEventListener("mousemove", ctdMove);
  window.addEventListener("mouseup", ctdUp);
  document.addEventListener("keydown", (e) => {
    if (activeTab() !== "charts" || e.target.matches("input, select, textarea") || $("dialog[open]")) return;
    if ((e.key === "Delete" || e.key === "Backspace") && CT.sel) { e.preventDefault(); ctdDelete(); }
    if (e.key === "Escape") { ctdTool("cursor"); ctdSelect(null); }
  });
  $("#ct-draw-delete").addEventListener("click", ctdDelete);
  $("#ct-draw-color").addEventListener("input", () => {
    const s = CT.sel;
    const line = s && ctdLines(s.sym).find((l) => l.id === s.id);
    if (!line) return;
    line.color = $("#ct-draw-color").value;
    ctdRepaint(s.sym);
    ctdSave(s.sym);
  });
  $("#ct-line-alert").addEventListener("click", () => {
    const s = CT.sel;
    const line = s && ctdLines(s.sym).find((l) => l.id === s.id);
    if (!line) return;
    const tf = ctActive().tf;
    alPrefill({ symbols: [s.sym], tf: /^\d+m$|^\d+h$|^1D$/.test(tf) ? tf : "5m", line: { ...line } });
  });
  $("#ct-risk-open").addEventListener("click", ctdRiskOpen);
  $("#ct-all-alerts").addEventListener("click", ctdAlertAll);
  $("#ct-risk-close").addEventListener("click", () => $("#ct-risk").close());
  $("#ct-risk-form").addEventListener("input", (e) => {
    if (e.target.id === "rk-side" || e.target.id === "rk-entry") ctdRiskStop();
    ctdRiskCompute();
  });
  $("#ct-risk-form").addEventListener("submit", (e) => e.preventDefault());
  setInterval(ctdLoadState, 30000);
  setInterval(ctgPoll, 10000);
  ctgPoll();
}

/* ---------- the Alerts tab ---------- */

const AL = { alerts: [], events: [], since: null, editing: null, started: false, ops: null };
const AL_OPS = { crosses_above: "crosses above", crosses_below: "crosses below", crosses: "crosses", above: "is above", below: "is below",
  retest: "breaks and is retested, holding (either way)", retest_up: "breaks up and is retested, holding", retest_down: "breaks down and is retested, holding" };
const AL_RETESTS = new Set(["retest", "retest_up", "retest_down"]);
const alSymbol = () => ($("#al-symbols").value.split(/[\s,]+/)[0] || "").trim().toUpperCase();

function alOperandHtml(side, x) {
  const kind = x?.kind || (side === "left" ? "price" : "value");
  const catalog = CT.catalog || [];
  const def = kind === "study" ? CT.catalogBy[x.type] || catalog[0] : null;
  const study = kind === "study" && def ? `<select class="al-study">${catalog.map((s) => `<option value="${esc(s.type)}" ${s.type === def.type ? "selected" : ""}>${esc(s.name)}</option>`).join("")}</select>
    ${def.outputs.length > 1 ? `<select class="al-output">${def.outputs.map((o) => `<option value="${esc(o.key)}" ${o.key === x.output ? "selected" : ""}>${esc(o.name)}</option>`).join("")}</select>` : ""}
    ${def.inputs.map((i) => i.kind === "source"
      ? `<select class="al-in" data-in="${esc(i.key)}" title="${esc(i.name)}">${CT_SOURCES.map((c) => `<option ${c === (x.inputs?.[i.key] ?? i.default) ? "selected" : ""}>${c}</option>`).join("")}</select>`
      : `<input class="al-in" data-in="${esc(i.key)}" type="number" step="any" title="${esc(i.name)}" value="${x.inputs?.[i.key] ?? i.default}" size="4">`).join("")}` : "";
  const sym = x?.symbol || alSymbol();
  const lines = kind === "line" ? ctdLines(sym) : [];
  const line = kind === "line" ? `<select class="al-line" data-sym="${esc(sym)}">${lines.length
    ? lines.map((l) => `<option value="${esc(l.id)}" ${l.id === x?.line?.id ? "selected" : ""}>${esc(ctdLabel(l))}</option>`).join("")
    : `<option value="">No lines on ${esc(sym || "this symbol")}: draw one on the Charts tab</option>`}</select>` : "";
  return `<span class="al-operand" data-side="${side}"><select class="al-kind">
      ${[["price", "Price"], ["volume", "Volume"], ["value", "Number"], ["study", "Indicator"], ["line", "Drawn line"]].map(([k, n]) => `<option value="${k}" ${k === kind ? "selected" : ""}>${n}</option>`).join("")}
    </select>${kind === "value" ? `<input class="al-value" type="number" step="any" value="${x?.value ?? ""}" placeholder="700.00" size="8">` : ""}${study}${line}</span>`;
}

function alCondHtml(c, k) {
  return `<div class="al-cond" data-k="${k}">${alOperandHtml("left", c.left)}
    <select class="al-op">${Object.entries(AL_OPS).map(([o, n]) => `<option value="${o}" ${o === c.op ? "selected" : ""}>${n}</option>`).join("")}</select>
    ${AL_RETESTS.has(c.op) ? `<span class="muted small">on closed candles</span>` : alOperandHtml("right", c.right)}<button type="button" class="ghost ct-x al-drop" title="Remove this condition">✕</button></div>`;
}

function alReadOperand(el) {
  if (!el) return null;
  const kind = $(".al-kind", el).value;
  if (kind === "line") {
    const sel = $(".al-line", el), sym = sel?.dataset.sym || alSymbol();
    const line = ctdLines(sym).find((l) => l.id === sel?.value);
    return { kind, symbol: sym, line: line ? { ...line } : null };
  }
  if (kind === "value") {
    const raw = ($(".al-value", el)?.value ?? "").trim();
    return { kind, value: raw === "" ? "" : Number(raw) };
  }
  if (kind !== "study") return { kind };
  const type = $(".al-study", el)?.value || CT.catalog?.[0]?.type;
  const def = CT.catalogBy[type];
  const inputs = {};
  for (const i of $$(".al-in", el)) inputs[i.dataset.in] = i.tagName === "SELECT" ? i.value : Number(i.value);
  return { kind, type, output: $(".al-output", el)?.value || def?.outputs[0].key, inputs };
}

function alReadConds() {
  return $$("#al-conds .al-cond").map((el) => ({ left: alReadOperand($('[data-side="left"]', el)), op: $(".al-op", el).value, right: alReadOperand($('[data-side="right"]', el)) }));
}

function alRenderConds(conds) {
  $("#al-conds").innerHTML = conds.map(alCondHtml).join("");
  $("#al-add-cond").disabled = conds.length >= 5;
}

function alFillWatchlists() {
  const sel = $("#al-watchlist");
  if (!sel || !CT.wl) return;
  const keep = sel.value;
  sel.innerHTML = CT.wl.lists.map((x) => `<option>${esc(x.name)}</option>`).join("");
  if (keep) sel.value = keep;
}

function alReset() {
  AL.editing = null;
  $("#al-form-title").textContent = "New alert";
  $("#al-save").textContent = "Create alert";
  $("#al-cancel").hidden = true;
  $("#al-name").value = $("#al-note").value = $("#al-expires").value = "";
  $("#al-trigger").value = "once";
  $("#al-phone").checked = true;
  $("#al-overnight").checked = false;
  alRenderConds([{ left: { kind: "price" }, op: "crosses_above", right: { kind: "value", value: "" } }]);
}

function alPrefill(opts = {}) {
  AL.pending = opts;
  showTab("alerts");
  if (AL.ready) alApplyPending();
}

function alApplyPending() {
  const opts = AL.pending;
  AL.pending = null;
  if (!opts) return;
  const { symbols, watchlist, tf, price, line, overnight } = opts;
  alReset();
  $("#al-overnight").checked = !!overnight || line?.clock === "24h";
  if (watchlist) { $('input[name="al-target"][value="watchlist"]').checked = true; alFillWatchlists(); $("#al-watchlist").value = watchlist; }
  else { $('input[name="al-target"][value="symbols"]').checked = true; $("#al-symbols").value = (symbols || []).join(", "); }
  if (tf) $("#al-tf").value = [...$("#al-tf").options].some((o) => o.value === tf) ? tf : "5m";
  if (price != null) alRenderConds([{ left: { kind: "price" }, op: "crosses_above", right: { kind: "value", value: Number(price).toFixed(2) } }]);
  if (line) {
    alRenderConds([{ left: { kind: "line", symbol: symbols[0], line }, op: "retest", right: null }]);
    $("#al-trigger").value = "bar";
  }
  $("#al-symbols").focus();
}

async function alEdit(a) {
  await Promise.all(a.conditions.flatMap((c) => [c.left, c.right]).filter((x) => x?.kind === "line").map((x) => ctdFetch(x.symbol)));
  AL.editing = a.id;
  $("#al-form-title").textContent = "Edit alert";
  $("#al-save").textContent = "Save alert";
  $("#al-cancel").hidden = false;
  $(`input[name="al-target"][value="${a.watchlist ? "watchlist" : "symbols"}"]`).checked = true;
  $("#al-symbols").value = (a.symbols || []).join(", ");
  alFillWatchlists();
  if (a.watchlist) $("#al-watchlist").value = a.watchlist;
  $("#al-tf").value = a.tf;
  $("#al-match").value = a.match;
  $("#al-trigger").value = a.trigger;
  $("#al-expires").value = a.expires ? a.expires.slice(0, 16) : "";
  $("#al-phone").checked = a.phone;
  $("#al-overnight").checked = a.session === "24h";
  $("#al-name").value = a.name || "";
  $("#al-note").value = a.note || "";
  alRenderConds(a.conditions);
  $("#al-form").scrollIntoView({ behavior: "smooth", block: "start" });
}

function alPayload() {
  const target = $('input[name="al-target"]:checked').value;
  return {
    symbols: target === "symbols" ? $("#al-symbols").value.split(/[\s,]+/).map((s) => s.trim().toUpperCase()).filter(Boolean) : [],
    watchlist: target === "watchlist" ? $("#al-watchlist").value : null,
    tf: $("#al-tf").value, match: $("#al-match").value, trigger: $("#al-trigger").value,
    expires: $("#al-expires").value || null, phone: $("#al-phone").checked, session: $("#al-overnight").checked ? "24h" : "ext",
    name: $("#al-name").value.trim(), note: $("#al-note").value.trim(), conditions: alReadConds(),
  };
}

function alRender() {
  $("#al-count").textContent = AL.alerts.length;
  const status = { active: ["ok", "Active"], paused: ["", "Paused"], triggered: ["warn", "Fired (once)"], expired: ["", "Expired"] };
  $("#al-table").innerHTML = AL.alerts.length ? `<tr><th></th><th>Alert</th><th>On</th><th>Fires</th><th>Fired</th><th></th></tr>` + AL.alerts.slice().reverse().map((a) => {
    const [cls, word] = status[a.status] || ["", a.status];
    return `<tr><td><span class="al-status ${cls}" title="${esc(word)}"></span></td>
      <td><b>${esc(a.name || a.description)}</b>${a.name ? `<div class="muted small">${esc(a.description)}</div>` : ""}${a.error ? `<div class="err-text small">${esc(a.error)}</div>` : ""}</td>
      <td>${a.watchlist ? `list <b>${esc(a.watchlist)}</b>` : esc((a.symbols || []).join(", "))}</td>
      <td class="small">${esc(AL.triggers?.[a.trigger] || a.trigger)}${a.expires ? `<div class="muted">until ${esc(a.expires.replace("T", " ").slice(0, 16))}</div>` : `<div class="muted">no end</div>`}</td>
      <td class="small">${a.count || 0}×${a.last ? `<div class="muted">${esc(hm(a.last))}</div>` : ""}</td>
      <td class="al-actions"><button type="button" class="ghost" data-act="${a.status === "active" ? "pause" : "resume"}" data-id="${a.id}">${a.status === "active" ? "Pause" : "Resume"}</button>
        <button type="button" class="ghost" data-act="edit" data-id="${a.id}">Edit</button>
        <button type="button" class="ghost danger" data-act="delete" data-id="${a.id}">Delete</button></td></tr>`;
  }).join("") : `<tr><td class="muted">No alerts yet. Make one on the left, or with the Alert button on a chart.</td></tr>`;
  $("#al-log").innerHTML = AL.events.length ? `<tr><th>Time</th><th>Symbol</th><th>What happened</th></tr>` + AL.events.slice(0, 100).map((e) =>
    `<tr><td class="num">${esc(new Date(e.at).toLocaleString("en-US", { timeZone: ET, month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false }))}</td>
      <td><b>${esc(e.symbol)}</b></td><td>${esc(e.message)}</td></tr>`).join("") : `<tr><td class="muted">Nothing has fired yet.</td></tr>`;
  $("#al-checking").textContent = AL.overnight ? "Checked every 15 seconds, Sunday 8 PM to Friday 8 PM ET (overnight alerts)"
    : AL.checking ? "Checking every 15 seconds" : "Checked 4:00 to 20:00 ET on weekdays";
  $("#al-desktop").hidden = !("Notification" in window) || Notification.permission !== "default";
}

async function alLoad() {
  try {
    const r = await api("/api/alerts");
    Object.assign(AL, { alerts: r.alerts, events: r.events, triggers: r.triggers, checking: r.checking, overnight: r.overnight });
    if (AL.since == null) AL.since = r.events[0]?.at || "";
    alRender();
  } catch (e) { toast("Alerts: " + e.message, true); }
}

// new matches, on any tab: a toast, the dot on the Alerts tab and a desktop alert if they're on
async function alPoll() {
  if (document.hidden || AL.since == null) return;
  try {
    const r = await api("/api/alerts?since=" + encodeURIComponent(AL.since));
    if (r.events.length) {
      AL.since = r.events[0].at;
      AL.events = [...r.events, ...AL.events.filter((e) => !r.events.some((n) => n.at === e.at && n.alert === e.alert && n.symbol === e.symbol))];
      for (const e of r.events.slice(0, 3)) {
        toast(`Alert: ${e.message}`);
        if ("Notification" in window && Notification.permission === "granted") new Notification(`Alert: ${e.symbol}`, { body: e.message, tag: `${e.alert}-${e.symbol}-${e.at}` });
      }
      if (activeTab() !== "alerts") $("#alerts-dot").hidden = false;
    }
    Object.assign(AL, { alerts: r.alerts, checking: r.checking });
    if (activeTab() === "alerts") alRender();
  } catch { /* next time */ }
}

async function alStart() {
  $("#alerts-dot").hidden = true;
  if (!AL.started) {
    AL.started = true;
    $("#al-add-cond").addEventListener("click", () => {
      const conds = alReadConds();
      if (conds.length >= 5) return;
      alRenderConds([...conds, { left: { kind: "price" }, op: "above", right: { kind: "value", value: "" } }]);
    });
    $("#al-conds").addEventListener("click", (e) => {
      if (!e.target.classList.contains("al-drop")) return;
      const conds = alReadConds();
      if (conds.length < 2) { toast("An alert needs a condition", true); return; }
      conds.splice(Number(e.target.closest(".al-cond").dataset.k), 1);
      alRenderConds(conds);
    });
    $("#al-conds").addEventListener("change", async (e) => {
      if (!e.target.matches(".al-kind, .al-study, .al-op")) return;
      const conds = alReadConds();
      const k = Number(e.target.closest(".al-cond").dataset.k);
      const side = e.target.closest(".al-operand")?.dataset.side;
      if (e.target.matches(".al-kind") && e.target.value === "study") conds[k][side] = { kind: "study", type: CT.catalog?.[0]?.type || "rsi", inputs: {} };
      if (e.target.matches(".al-study")) conds[k][side] = { kind: "study", type: e.target.value, inputs: {} };
      if (e.target.matches(".al-kind") && e.target.value === "line") await ctdFetch(alSymbol());
      if (e.target.matches(".al-op") && !conds[k].right) conds[k].right = { kind: "price" };
      alRenderConds(conds);
    });
    $("#al-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const body = alPayload();
      if ($$("#al-conds .al-value").some((i) => i.value.trim() === "" || !Number.isFinite(Number(i.value)))) {
        toast("Enter the number to compare with", true);
        return;
      }
      try {
        if (AL.editing) await api(`/api/alerts/${AL.editing}`, { body: { action: "edit", alert: body } });
        else await api("/api/alerts", { body });
        toast(AL.editing ? "Alert saved" : "Alert created");
        alReset();
        await alLoad();
      } catch (err) { toast(err.message, true); }
    });
    $("#al-cancel").addEventListener("click", alReset);
    $("#al-desktop").addEventListener("click", () => Notification.requestPermission().then(alRender));
    $("#al-table").addEventListener("click", async (e) => {
      const b = e.target.closest("button[data-act]");
      if (!b) return;
      const a = AL.alerts.find((x) => x.id === b.dataset.id);
      if (b.dataset.act === "edit") { alEdit(a); return; }
      if (b.dataset.act === "delete" && !confirm(`Delete the alert "${a.name || a.description}"?`)) return;
      try { AL.alerts = (await api(`/api/alerts/${b.dataset.id}`, { body: { action: b.dataset.act } })).alerts; alRender(); }
      catch (err) { toast(err.message, true); }
    });
    await ctCatalog();
    if (!CT.wl) await ctLoadWatchlists();
    alReset();
    AL.ready = true;
  }
  alFillWatchlists();
  alApplyPending();
  await alLoad();
}

loaders.alerts = alStart;

/* ---------- start ---------- */

if (activeTab() === "charts") ctStart();
if (activeTab() === "alerts") alStart();
api("/api/alerts").then((r) => { AL.since = r.events[0]?.at || ""; }).catch(() => { AL.since = ""; });
setInterval(alPoll, 15000);
