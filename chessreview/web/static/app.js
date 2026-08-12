"use strict";

/* ---------------------------------------------------------------- helpers */

const $ = (sel) => document.querySelector(sel);
const SVG_NS = "http://www.w3.org/2000/svg";

function h(tag, attrs, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "html") node.innerHTML = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(node.dataset, v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    node.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return node;
}

/* replaceChildren() and append() coerce a null argument into the literal string
   "null", which then shows up on the page. Always route conditional children
   through these instead of calling the DOM methods with a raw ternary. */
const clean = (kids) => kids.flat().filter(
  (k) => k !== null && k !== undefined && k !== false);
const fill = (node, ...kids) => (node.replaceChildren(...clean(kids)), node);
const add = (node, ...kids) => (node.append(...clean(kids)), node);

function s(tag, attrs, ...kids) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    node.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid) node.append(kid);
  return node;
}

const num = (v, d = 1) => (v === null || v === undefined ? "-" : Number(v).toFixed(d));
const pct = (v, d = 1) => (v === null || v === undefined ? "-" : Number(v).toFixed(d) + "%");
const int = (v) => (v === null || v === undefined ? "-" : String(Math.round(v)));

function date(ts) {
  if (!ts) return "-";
  return new Date(ts * 1000).toLocaleDateString(undefined, {
    year: "numeric", month: "short", day: "numeric",
  });
}

function ago(ts) {
  if (!ts) return "never";
  const secs = Math.max(0, Math.floor(Date.now() / 1000) - ts);
  if (secs < 60) return "just now";
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}

function until(ts) {
  if (!ts) return "";
  const secs = ts - Math.floor(Date.now() / 1000);
  if (secs <= 0) return "due now";
  if (secs < 3600) return `in ${Math.ceil(secs / 60)}m`;
  return `in ${Math.round(secs / 3600)}h`;
}

let toastTimer = null;
function toast(message, isError) {
  const old = $(".toast");
  if (old) old.remove();
  const node = h("div", { class: "toast" + (isError ? " err" : "") }, message);
  document.body.append(node);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.remove(), 4000);
}

async function api(path, params, options) {
  const url = new URL(path, location.origin);
  for (const [k, v] of Object.entries(params || {})) {
    if (v !== null && v !== undefined && v !== "") url.searchParams.set(k, v);
  }
  const res = await fetch(url, options);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) { /* text body */ }
    throw new Error(detail);
  }
  return res.headers.get("content-type")?.includes("json") ? res.json() : res.text();
}

const post = (path, body) => api(path, null, {
  method: "POST",
  headers: { "content-type": "application/json" },
  body: JSON.stringify(body || {}),
});

/* ------------------------------------------------------------------ state */

const state = {
  tab: "overview",
  config: null,
  filters: { time_class: "", color: "", since: "", until: "", rated_only: false, min_games: 3 },
};

function filterParams() {
  const f = state.filters;
  return {
    time_class: f.time_class, color: f.color, since: f.since, until: f.until,
    rated_only: f.rated_only ? "true" : "", min_games: f.min_games,
  };
}

/* ----------------------------------------------------------------- charts */

/* Multi-line trend. Series are direct-labeled at the line end and repeated in
   a legend, so identity never rests on color alone. */
function lineChart(opts) {
  const { series, formatY = int, formatX = (v) => v, height = 300 } = opts;
  const W = 820, H = height, pad = { l: 48, r: 76, t: 14, b: 26 };
  const wrap = h("div", { class: "chart" });
  const labels = [...new Set(series.flatMap((ser) => ser.points.map((p) => p.x)))].sort();
  if (!labels.length) return h("div", { class: "empty" }, "Nothing to plot yet.");
  const index = new Map(labels.map((l, i) => [l, i]));

  const values = series.flatMap((ser) => ser.points.map((p) => p.y)).filter((v) => v !== null);
  let lo = Math.min(...values), hi = Math.max(...values);
  if (lo === hi) { lo -= 1; hi += 1; }
  const span = hi - lo;
  lo -= span * 0.12; hi += span * 0.12;
  if (opts.zeroFloor && lo < 0) lo = 0;

  const X = (i) => pad.l + (labels.length === 1 ? 0 : (i / (labels.length - 1)) * (W - pad.l - pad.r));
  const Y = (v) => pad.t + (1 - (v - lo) / (hi - lo)) * (H - pad.t - pad.b);

  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, role: "img" });

  for (let i = 0; i <= 4; i++) {
    const v = lo + ((hi - lo) * i) / 4;
    const y = Y(v);
    svg.append(s("line", { class: "gridline", x1: pad.l, x2: W - pad.r, y1: y, y2: y }));
    svg.append(s("text", { x: pad.l - 8, y: y + 4, "text-anchor": "end" }, document.createTextNode(formatY(v))));
  }
  svg.append(s("line", { class: "axisline", x1: pad.l, x2: W - pad.r, y1: Y(lo), y2: Y(lo) }));

  const ticks = Math.min(6, labels.length);
  for (let i = 0; i < ticks; i++) {
    const idx = Math.round((i / Math.max(1, ticks - 1)) * (labels.length - 1));
    svg.append(s("text", {
      x: X(idx), y: H - 8,
      "text-anchor": i === 0 ? "start" : i === ticks - 1 ? "end" : "middle",
    }, document.createTextNode(formatX(labels[idx]))));
  }

  series.forEach((ser, si) => {
    const color = `var(--series-${(si % 4) + 1})`;
    const pts = ser.points.filter((p) => p.y !== null).map((p) => [X(index.get(p.x)), Y(p.y)]);
    if (!pts.length) return;
    svg.append(s("path", {
      class: "line", stroke: color,
      d: pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(" "),
    }));
    if (pts.length === 1) {
      svg.append(s("circle", { class: "marker", cx: pts[0][0], cy: pts[0][1], r: 4, fill: color }));
    }
    const last = pts[pts.length - 1];
    svg.append(s("text", {
      class: "serieslabel", x: last[0] + 8, y: last[1] + 4, fill: color,
    }, document.createTextNode(ser.name)));
  });

  const tip = h("div", { class: "tooltip", style: "display:none" });
  const cross = s("line", { class: "crosshair", y1: pad.t, y2: H - pad.b, style: "display:none" });
  svg.append(cross);
  const hit = s("rect", {
    x: pad.l, y: pad.t, width: W - pad.l - pad.r, height: H - pad.t - pad.b,
    fill: "transparent", style: "cursor:crosshair",
  });
  svg.append(hit);

  hit.addEventListener("mousemove", (ev) => {
    const box = svg.getBoundingClientRect();
    const px = ((ev.clientX - box.left) / box.width) * W;
    const ratio = (px - pad.l) / (W - pad.l - pad.r);
    const idx = Math.max(0, Math.min(labels.length - 1, Math.round(ratio * (labels.length - 1))));
    cross.setAttribute("x1", X(idx));
    cross.setAttribute("x2", X(idx));
    cross.style.display = "";
    const label = labels[idx];
    const lines = series
      .map((ser, si) => {
        const point = ser.points.find((p) => p.x === label);
        return point ? `<div><i style="display:inline-block;width:8px;height:8px;border-radius:2px;background:var(--series-${(si % 4) + 1});margin-right:6px"></i><span class="k">${ser.name}</span> <b>${formatY(point.y)}</b></div>` : null;
      })
      .filter(Boolean);
    if (!lines.length) { tip.style.display = "none"; return; }
    tip.innerHTML = `<div class="k" style="margin-bottom:4px">${formatX(label)}</div>${lines.join("")}`;
    tip.style.display = "";
    const wrapBox = wrap.getBoundingClientRect();
    const left = ((X(idx) / W) * wrapBox.width) + 12;
    tip.style.left = Math.min(left, wrapBox.width - tip.offsetWidth - 8) + "px";
    tip.style.top = "8px";
  });
  hit.addEventListener("mouseleave", () => { cross.style.display = "none"; tip.style.display = "none"; });

  wrap.append(svg, tip);
  if (series.length > 1) {
    wrap.append(h("div", { class: "legend" }, series.map((ser, si) =>
      h("span", {}, h("i", { style: `background:var(--series-${(si % 4) + 1})` }), ser.name))));
  }
  return wrap;
}

/* Horizontal bars. Magnitude against a common baseline, one hue. */
function hbars(rows, opts) {
  const { label, value, format = num, sub, color = "var(--series-1)", max } = opts || {};
  if (!rows.length) return h("div", { class: "empty" }, "No data yet.");
  const top = max ?? Math.max(...rows.map((r) => value(r) || 0), 0.0001);
  return h("div", { class: "bars" }, rows.map((row) => {
    const v = value(row) || 0;
    return h("div", { class: "barrow" },
      h("div", { class: "name", title: label(row) }, label(row)),
      h("div", { class: "track" },
        h("div", { class: "fill", style: `width:${Math.max(0, (v / top) * 100)}%;background:${typeof color === "function" ? color(row) : color}` })),
      h("div", { class: "val" }, format(v),
        sub ? h("div", { class: "sub" }, sub(row)) : null));
  }));
}

function table(columns, rows, opts) {
  const { onRow } = opts || {};
  if (!rows.length) return h("div", { class: "empty" }, "No rows match these filters.");
  return h("div", { class: "scroll" },
    h("table", {},
      h("thead", {}, h("tr", {}, columns.map((c) =>
        h("th", { class: c.num ? "num" : null }, c.title)))),
      h("tbody", {}, rows.map((row) =>
        h("tr", {
          class: onRow ? "clickable" : null,
          onclick: onRow ? () => onRow(row) : null,
        }, columns.map((c) => {
          const cell = c.render(row);
          return h("td", { class: c.num ? "num" : null }, cell === null || cell === undefined ? "-" : cell);
        }))))));
}

function kpi(label, value, sub, hero) {
  return h("div", { class: "kpi" + (hero ? " hero" : "") },
    h("div", { class: "label" }, label),
    h("div", { class: "value" }, value),
    sub ? h("div", { class: "sub" }, sub) : null);
}

function card(title, note, ...body) {
  return h("div", { class: "card" },
    h("h2", {}, title),
    note ? h("div", { class: "note" }, note) : null,
    ...body);
}

/* ------------------------------------------------------------- chessboard */

const GLYPH = { k: "♚", q: "♛", r: "♜", b: "♝", n: "♞", p: "♟" };

/* Square name -> position on screen, in units of one square, honouring flip.
   Not flipped, a8 is the top-left cell; flipped, a1 is the top-right. */
function squareXY(square, flip) {
  const file = "abcdefgh".indexOf(square[0]);
  const rank = Number(square[1]);
  if (file < 0 || !rank) return null;
  return {
    x: (flip ? 7 - file : file) + 0.5,
    y: (flip ? rank - 1 : 8 - rank) + 0.5,
  };
}

/* One arrow from the origin square to the destination square. Straight, even
   for knights: the pair of squares is what matters, not the path. */
function arrow(from, to, color, flip) {
  const a = squareXY(from, flip), b = squareXY(to, flip);
  if (!a || !b) return null;
  const dx = b.x - a.x, dy = b.y - a.y;
  const len = Math.hypot(dx, dy);
  if (!len) return null;
  const ux = dx / len, uy = dy / len;
  const head = 0.34, halfWidth = 0.19;
  // Stop the tip just short of the square's centre, and end the shaft where
  // the head begins so the two do not overlap through the transparency.
  const tipX = b.x - ux * 0.06, tipY = b.y - uy * 0.06;
  const baseX = tipX - ux * head, baseY = tipY - uy * head;
  const px = -uy, py = ux;
  return s("g", { fill: color, stroke: color, opacity: 0.85 },
    s("line", {
      x1: a.x, y1: a.y, x2: baseX, y2: baseY,
      "stroke-width": 0.13, "stroke-linecap": "round",
    }),
    s("polygon", {
      stroke: "none",
      points: [
        `${tipX},${tipY}`,
        `${baseX + px * halfWidth},${baseY + py * halfWidth}`,
        `${baseX - px * halfWidth},${baseY - py * halfWidth}`,
      ].join(" "),
    }));
}

function board(fen, opts) {
  const { flip = false, highlight = [], highlight2 = [], arrows = [] } = opts || {};
  const placement = (fen || "").split(" ")[0];
  const grid = h("div", { class: "board" });
  const ranks = placement.split("/");
  const order = flip ? [...ranks].reverse() : ranks;
  order.forEach((rank, ri) => {
    const cells = [];
    for (const ch of rank) {
      if (/\d/.test(ch)) { for (let i = 0; i < Number(ch); i++) cells.push(null); }
      else cells.push(ch);
    }
    const row = flip ? cells.reverse() : cells;
    row.forEach((piece, fi) => {
      const rankNo = flip ? ri + 1 : 8 - ri;
      const fileNo = flip ? 7 - fi : fi;
      const square = "abcdefgh"[fileNo] + rankNo;
      // a1 (file 0, rank 1) is dark, so a square is light when file+rank is even.
      const light = (rankNo + fileNo) % 2 === 0;
      const classes = ["", light ? "l" : "d"];
      if (highlight.includes(square)) classes.push("hl");
      if (highlight2.includes(square)) classes.push("hl2");
      const cell = h("div", { class: classes.join(" ").trim(), title: square });
      if (piece) {
        cell.append(h("span", {
          class: piece === piece.toUpperCase() ? "piece-w" : "piece-b",
        }, GLYPH[piece.toLowerCase()]));
      }
      grid.append(cell);
    });
  });

  const overlay = s("svg", {
    class: "board-arrows", viewBox: "0 0 8 8",
    preserveAspectRatio: "xMidYMid meet", "aria-hidden": "true",
  });
  for (const a of arrows) {
    const node = arrow(a.from, a.to, a.color, flip);
    if (node) overlay.append(node);
  }
  return h("div", { class: "board-wrap" }, grid, overlay);
}

const squaresOf = (uci) => (uci ? [uci.slice(0, 2), uci.slice(2, 4)] : []);

/* Played move in yellow, engine's preference in green, matching the squares. */
function moveArrows(uci, bestUci) {
  const out = [];
  if (uci) out.push({ from: uci.slice(0, 2), to: uci.slice(2, 4), color: "var(--warning)" });
  if (bestUci && bestUci !== uci) {
    out.push({ from: bestUci.slice(0, 2), to: bestUci.slice(2, 4), color: "var(--good)" });
  }
  return out;
}

/* ------------------------------------------------------------------- tabs */

async function renderOverview(view) {
  const params = filterParams();
  const [summary, ratings, trend] = await Promise.all([
    api("/api/summary", params),
    api("/api/ratings", { ...params, bucket: "day" }),
    api("/api/trend", { ...params, bucket: "month" }),
  ]);
  const t = summary.totals;
  view.replaceChildren();

  if (!t.games) {
    view.append(h("div", { class: "empty" },
      "No games stored yet. Set your username in Settings, then hit Sync now."));
    return;
  }

  view.append(h("div", { class: "kpis", style: "margin-bottom:16px" },
    kpi("Games", int(t.games), `${date(t.first_at)} to ${date(t.last_at)}`),
    kpi("Score", pct(t.score), `${t.wins}W ${t.losses}L ${t.draws}D`, true),
    kpi("Accuracy", t.accuracy === null ? "-" : pct(t.accuracy), `${t.analyzed} games analyzed`),
    kpi("ACPL", int(t.acpl), t.opp_acpl ? `opponents ${int(t.opp_acpl)}` : null),
    kpi("Blunders / game", num(t.blunders_per_game, 2), "your moves only")));

  const grid = h("div", { class: "grid" });

  grid.append(h("div", { class: "col-8" },
    card("Rating over time", "Daily average per time control, rated games only.",
      ratings.series.length
        ? lineChart({
            series: ratings.series.map((ser) => ({
              name: ser.name,
              points: ser.points.map((p) => ({ x: p.date, y: p.rating })),
            })),
            formatY: int,
          })
        : h("div", { class: "empty" }, "No rated games with ratings recorded."))));

  const wld = [
    { k: "Wins", v: t.wins, c: "var(--win)" },
    { k: "Draws", v: t.draws, c: "var(--draw)" },
    { k: "Losses", v: t.losses, c: "var(--loss)" },
  ];
  grid.append(h("div", { class: "col-4" },
    card("Results", `${t.games} games`,
      h("div", { class: "stacked" }, wld.filter((x) => x.v).map((x) =>
        h("span", { style: `width:${(x.v / t.games) * 100}%;background:${x.c}`, title: `${x.k}: ${x.v}` }))),
      h("div", { class: "legend" }, wld.map((x) =>
        h("span", {}, h("i", { style: `background:${x.c}` }), `${x.k} ${x.v}`))),
      h("div", { style: "margin-top:16px" },
        hbars(summary.by_time_class, {
          label: (r) => r.k || "unknown",
          value: (r) => r.score,
          format: (v) => pct(v, 0),
          sub: (r) => `${r.games} games`,
        })))));

  grid.append(h("div", { class: "col-6" },
    card("Score by month", "Half a point for a draw, so 50% is breaking even.",
      lineChart({
        series: [{ name: "score", points: trend.rows.map((r) => ({ x: r.bucket, y: r.score })) }],
        formatY: (v) => v.toFixed(0) + "%",
        height: 260,
      }))));

  grid.append(h("div", { class: "col-6" },
    card("How your games end",
      "A pile of timeouts is a clock problem, not a chess problem.",
      table([
        { title: "Result", render: (r) => h("span", { class: "res-" + r.result }, r.result) },
        { title: "Termination", render: (r) => r.termination },
        { title: "Games", num: true, render: (r) => r.games },
        { title: "Share", num: true, render: (r) => pct(r.share, 0) },
      ], summary.endings))));

  view.append(grid);
}

async function renderMistakes(view) {
  const params = filterParams();
  const [mq, clock, sessions, opponents] = await Promise.all([
    api("/api/mistakes", params),
    api("/api/clock", params),
    api("/api/sessions", { ...params, gap: 60 }),
    api("/api/opponents", params),
  ]);
  view.replaceChildren();

  if (!mq.games) {
    view.append(h("div", { class: "empty" },
      "Nothing analyzed yet. Hit Analyze and come back."));
    return;
  }

  const you = mq.by_who.find((r) => r.who === "You") || {};
  const them = mq.by_who.find((r) => r.who === "Opponents") || {};
  view.append(h("div", { class: "kpis", style: "margin-bottom:16px" },
    kpi("Analyzed games", int(mq.games)),
    kpi("Your blunders / game", num(you.blunders_per_game, 2),
      `opponents ${num(them.blunders_per_game, 2)}`, true),
    kpi("Your mistakes", int(you.mistakes), `${int(you.inaccuracies)} inaccuracies`),
    kpi("Your ACPL", int(you.acpl), `opponents ${int(them.acpl)}`)));

  const grid = h("div", { class: "grid" });

  grid.append(h("div", { class: "col-6" },
    card("Blunder rate by time remaining",
      "If this climbs to the right, your problem is the clock.",
      clock.rows.length
        ? hbars(clock.rows, {
            label: (r) => r.bucket,
            value: (r) => r.blunder_rate,
            format: (v) => pct(v, 1),
            sub: (r) => `${r.blunders} of ${r.moves} moves`,
          })
        : h("div", { class: "empty" }, "No clock data in these games."))));

  grid.append(h("div", { class: "col-6" },
    card("Blunder rate by move number", "Where in the game it goes wrong.",
      hbars(mq.by_move, {
        label: (r) => "moves " + r.bucket,
        value: (r) => r.blunder_rate,
        format: (v) => pct(v, 1),
        sub: (r) => `${r.blunders} of ${r.moves} moves`,
      }))));

  grid.append(h("div", { class: "col-6" },
    card("Errors by phase", "Endgame is decided by material left, not move number.",
      hbars(mq.by_phase, {
        label: (r) => r.phase,
        value: (r) => r.blunder_rate,
        format: (v) => pct(v, 1),
        sub: (r) => `${r.blunders} blunders, ${r.mistakes} mistakes`,
      }))));

  grid.append(h("div", { class: "col-6" },
    card("Score by game number in a session",
      "A new session starts after 60 minutes idle. A slide to the right is tilt.",
      sessions.rows.length
        ? hbars(sessions.rows, {
            label: (r) => "game " + r.bucket,
            value: (r) => r.score,
            format: (v) => pct(v, 0),
            sub: (r) => `${r.games} games`,
          })
        : h("div", { class: "empty" }, "Not enough games yet."))));

  grid.append(h("div", { class: "col-6" },
    card("Performance by opponent strength", null,
      opponents.rows.length
        ? table([
            { title: "Opponent", render: (r) => r.bucket },
            { title: "Games", num: true, render: (r) => r.games },
            { title: "W-L-D", num: true, render: (r) => `${r.wins}-${r.losses}-${r.draws}` },
            { title: "Score", num: true, render: (r) => pct(r.score, 0) },
            { title: "ACPL", num: true, render: (r) => int(r.acpl) },
          ], opponents.rows)
        : h("div", { class: "empty" }, "No rated games with both ratings."))));

  grid.append(h("div", { class: "col-6" },
    card("You vs your opponents", "Same engine, same depth, both sides.",
      table([
        { title: "", render: (r) => r.who },
        { title: "Moves", num: true, render: (r) => r.moves },
        { title: "Blunders", num: true, render: (r) => r.blunders },
        { title: "Mistakes", num: true, render: (r) => r.mistakes },
        { title: "Inaccuracies", num: true, render: (r) => r.inaccuracies },
        { title: "ACPL", num: true, render: (r) => int(r.acpl) },
      ], mq.by_who))));

  view.append(grid);
}

async function renderOpenings(view) {
  const params = filterParams();
  const data = await api("/api/openings", { ...params, limit: 60 });
  view.replaceChildren();
  const rows = data.rows;
  view.append(card(
    `Openings, worst score first`,
    `Only lines with at least ${state.filters.min_games} games. Change that in the filter bar.`,
    table([
      { title: "Opening", render: (r) => r.opening },
      { title: "Color", render: (r) => h("span", { class: "tag" }, r.color) },
      { title: "Games", num: true, render: (r) => r.games },
      { title: "W-L-D", num: true, render: (r) => `${r.wins}-${r.losses}-${r.draws}` },
      {
        title: "Score", num: true, render: (r) => h("span", {},
          pct(r.score, 0),
          h("span", { class: "minibar" }, h("span", {
            style: `width:${Math.max(2, r.score)}%;background:${r.score < 40 ? "var(--critical)" : r.score > 60 ? "var(--good)" : "var(--series-1)"}`,
          }))),
      },
      { title: "ACPL", num: true, render: (r) => int(r.acpl) },
    ], rows)));
}

async function renderBlunders(view) {
  const params = filterParams();
  const data = await api("/api/blunders", { ...params, limit: 24 });
  view.replaceChildren();
  if (!data.rows.length) {
    view.append(h("div", { class: "empty" },
      "No blunders found. Either nothing is analyzed yet, or you have been playing well."));
    return;
  }
  view.append(h("div", { class: "note", style: "font-size:12px;color:var(--text-muted);margin-bottom:14px" },
    "The position before your move. Yellow is what you played, green is what the engine wanted."));
  view.append(h("div", { class: "blunder-grid" }, data.rows.map((r) => {
    const dots = r.side === "black" ? "..." : ".";
    return h("div", { class: "blunder-card" },
      h("div", { class: "meta" },
        h("span", {}, `${r.move_no}${dots} ${r.san}`),
        h("span", {}, `-${num(r.winp_loss, 0)}% win`)),
      board(r.fen_before, {
        flip: r.color === "black",
        highlight: squaresOf(r.uci),
        highlight2: squaresOf(r.best_uci),
        arrows: moveArrows(r.uci, r.best_uci),
      }),
      h("div", { class: "moves" },
        h("span", {}, "played ", h("span", { class: "played" }, r.san)),
        h("span", {}, "better ", h("span", { class: "best" }, r.best_san || "?"))),
      h("div", { class: "meta", style: "margin-top:8px;margin-bottom:0" },
        h("span", { title: r.opening || "" }, (r.opening || "unknown").slice(0, 26)),
        h("button", { class: "icon", onclick: () => openGame(r.uuid) }, "Open")));
  })));
}

async function renderGames(view) {
  const params = filterParams();
  const data = await api("/api/games", { ...params, limit: 100 });
  view.replaceChildren();
  view.append(card(
    `Games`, `${data.total} match these filters. Click a row to review it.`,
    table([
      { title: "Date", render: (r) => date(r.played_at) },
      { title: "Class", render: (r) => h("span", { class: "tag" }, r.time_class || "?") },
      { title: "Color", render: (r) => r.color },
      { title: "Result", render: (r) => h("span", { class: "res-" + r.result }, r.result) },
      { title: "How", render: (r) => r.termination || "-" },
      { title: "Rating", num: true, render: (r) => r.my_rating },
      { title: "Opp", num: true, render: (r) => r.opp_rating },
      { title: "ACPL", num: true, render: (r) => (r.analyzed_at ? int(r.my_acpl) : h("span", { class: "tag" }, "pending")) },
      { title: "Opening", render: (r) => (r.opening || "").slice(0, 40) },
    ], data.games, { onRow: (r) => openGame(r.uuid) })));
}

const TABS = {
  overview: renderOverview,
  mistakes: renderMistakes,
  openings: renderOpenings,
  blunders: renderBlunders,
  games: renderGames,
};

async function render() {
  const view = $("#view");
  view.replaceChildren(h("div", { class: "empty" }, h("span", { class: "spinner" })));
  try {
    await TABS[state.tab](view);
  } catch (err) {
    view.replaceChildren(h("div", { class: "empty" }, "Could not load: " + err.message));
  }
}

/* ------------------------------------------------------------- game dialog */

/* scrollIntoView scrolls every scrollable ancestor, not just the nearest one,
   so using it on the move list also dragged the dialog body and pushed the
   board off the top. This moves the list and nothing else. */
function keepInView(container, node) {
  if (!container) return;
  const box = container.getBoundingClientRect();
  const item = node.getBoundingClientRect();
  // The column header is sticky at the top of the list, so the usable area
  // starts below it.
  const head = container.querySelector(".colhead");
  const top = box.top + (head ? head.getBoundingClientRect().height : 0);
  if (item.top < top) {
    container.scrollTop -= top - item.top;
  } else if (item.bottom > box.bottom) {
    container.scrollTop += item.bottom - box.bottom;
  }
}

let gameKeys = null;   // keydown handler belonging to the open game dialog

/* A follow-up thread hanging off one explanation. `ply` is null for the
   whole-game thread. Only rendered once an explanation exists, because the
   conversation continues from it. */
function chatThread(uuid, ply, placeholder) {
  const log = h("div", { class: "chatlog" });
  const input = h("input", {
    type: "text", class: "chatinput", placeholder,
    onkeydown: (ev) => { if (ev.key === "Enter") send(); ev.stopPropagation(); },
  });
  const sendBtn = h("button", { class: "primary", onclick: () => send() }, "Ask");
  const clearBtn = h("button", { title: "Delete this conversation",
    onclick: async () => {
      await post("/api/chat/clear", { uuid, ply });
      messages = [];
      draw();
    } }, "Clear");
  const row = h("div", { class: "chatrow" }, input, sendBtn, clearBtn);

  let messages = [];
  let busy = false;

  function draw() {
    fill(log, messages.map((m) =>
      h("div", { class: "chatmsg " + m.role },
        h("span", { class: "who" }, m.role === "user" ? "you" : "coach"),
        h("span", { class: "text" }, m.content))));
    log.scrollTop = log.scrollHeight;
  }

  async function send() {
    const text = input.value.trim();
    if (!text || busy) return;
    busy = true;
    input.value = "";
    messages.push({ role: "user", content: text });
    messages.push({ role: "assistant", content: "…" });
    draw();
    sendBtn.disabled = input.disabled = true;
    try {
      const res = await post("/api/chat", { uuid, ply, message: text });
      messages[messages.length - 1] = { role: "assistant", content: res.text };
      if (res.candidates && res.candidates.length) {
        messages[messages.length - 1].content +=
          `\n(engine-checked: ${res.candidates.join(", ")})`;
      }
    } catch (err) {
      messages[messages.length - 1] = { role: "assistant", content: "⚠ " + err.message };
    } finally {
      busy = false;
      sendBtn.disabled = input.disabled = false;
      draw();
      input.focus();
    }
  }

  async function load() {
    try {
      const res = await api("/api/chat", { uuid, ply: ply === null ? "" : ply });
      messages = res.messages.map((m) => ({ role: m.role, content: m.content }));
      draw();
    } catch (err) { /* an empty thread is fine */ }
  }

  return { log, row, load, reset: () => { messages = []; draw(); } };
}

/* Two different alphabets share one string, which is what trips people up.
   The ? marks are this tool's judgment of the move; +, #, x and the rest are
   part of standard notation and say nothing about whether a move was good. */
const KEY_QUALITY = [
  ["!!", "brilliant", "a sound sacrifice you were not already winning without", "var(--series-3)"],
  ["!", "great", "the only move that held the position", "var(--series-1)"],
  ["??", "blunder", "throws away 20+ points of win probability", "var(--critical)"],
  ["?", "mistake", "costs 10 to 20 points", "var(--warning)"],
  ["?!", "inaccuracy", "costs 5 to 10 points, still playable", "var(--series-1)"],
];

const KEY_NOTATION = [
  ["+", "check"],
  ["#", "checkmate, the game ends"],
  ["x", "a capture, as in Bxa6"],
  ["=Q", "a pawn promoted, here to a queen"],
  ["O-O", "castled kingside (O-O-O is queenside)"],
  ["2...", "the dots mean it is Black's half of move 2"],
  ["(-29)", "win probability you gave away on that move"],
  ["(+29)", "win probability you gained, when your opponent erred"],
];

function symbolKey() {
  const open = localStorage.getItem("symbolkey") !== "closed";
  const col = (title, ...rows) =>
    h("div", { class: "keycol" }, h("div", { class: "keygroup" }, title),
      h("dl", {}, ...rows));
  const node = h("details", { class: "symbolkey", open: open || null },
    h("summary", {}, "What the symbols mean"),
    h("div", { class: "keycols" },
      col("Move quality, added by the analysis",
        KEY_QUALITY.flatMap(([sym, name, why, color]) => [
          h("dt", { style: `color:${color}` }, sym),
          h("dd", {}, h("b", {}, name), " — ", why),
        ])),
      col("Standard notation, not a judgment",
        KEY_NOTATION.flatMap(([sym, meaning]) => [
          h("dt", {}, sym), h("dd", {}, meaning),
        ])),
      col("On the board",
        h("dt", { style: "color:var(--warning)" }, "▬"),
        h("dd", {}, "the move that was played"),
        h("dt", { style: "color:var(--good)" }, "▬"),
        h("dd", {}, "what the engine preferred, shown only on your errors"),
        h("dt", { style: "color:var(--text-muted)" }, "dim"),
        h("dd", {}, "in the move list, your opponent's moves"))),
    h("div", { class: "keynote" },
      "The board shows the position before the move, so the piece is still on "
      + "the square it is leaving."));
  node.addEventListener("toggle", () =>
    localStorage.setItem("symbolkey", node.open ? "open" : "closed"));
  return node;
}

/* Win probability from the tracked player's point of view, 50 = even. */
function playerWinPercent(move, playerColor) {
  const mine = move.side === playerColor;
  return mine ? move.winp_after : 100 - move.winp_after;
}

function evalChart(moves, playerColor, onPick) {
  const W = 820, H = 180, pad = { l: 34, r: 8, t: 10, b: 18 };
  const wrap = h("div", { class: "chart" });
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}` });
  const X = (i) => pad.l + (moves.length < 2 ? 0 : (i / (moves.length - 1)) * (W - pad.l - pad.r));
  const Y = (v) => pad.t + (1 - v / 100) * (H - pad.t - pad.b);

  svg.append(s("line", { class: "gridline", x1: pad.l, x2: W - pad.r, y1: Y(100), y2: Y(100) }));
  svg.append(s("line", { class: "axisline", x1: pad.l, x2: W - pad.r, y1: Y(50), y2: Y(50) }));
  svg.append(s("line", { class: "gridline", x1: pad.l, x2: W - pad.r, y1: Y(0), y2: Y(0) }));
  [[100, "win"], [50, "even"], [0, "lost"]].forEach(([v, label]) =>
    svg.append(s("text", { x: pad.l - 6, y: Y(v) + 4, "text-anchor": "end" },
      document.createTextNode(label))));

  const pts = moves.map((m, i) => [X(i), Y(playerWinPercent(m, playerColor))]);
  const line = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(" ");
  const areaTop = `${line} L${pts[pts.length - 1][0]} ${Y(50)} L${pts[0][0]} ${Y(50)} Z`;

  svg.append(s("clipPath", { id: "clip-above" },
    s("rect", { x: pad.l, y: pad.t, width: W - pad.l - pad.r, height: Y(50) - pad.t })));
  svg.append(s("clipPath", { id: "clip-below" },
    s("rect", { x: pad.l, y: Y(50), width: W - pad.l - pad.r, height: H - pad.b - Y(50) })));
  svg.append(s("path", { d: areaTop, fill: "var(--series-1)", opacity: 0.22, "clip-path": "url(#clip-above)" }));
  svg.append(s("path", { d: areaTop, fill: "var(--loss)", opacity: 0.22, "clip-path": "url(#clip-below)" }));
  svg.append(s("path", { class: "line", d: line, stroke: "var(--text-secondary)", "stroke-width": 1.5 }));

  moves.forEach((m, i) => {
    if (!m.is_player || !["blunder", "mistake"].includes(m.judgment)) return;
    svg.append(s("circle", {
      class: "marker", cx: pts[i][0], cy: pts[i][1], r: 4.5,
      fill: m.judgment === "blunder" ? "var(--critical)" : "var(--warning)",
    }));
  });

  const hit = s("rect", { x: 0, y: 0, width: W, height: H, fill: "transparent", style: "cursor:crosshair" });
  const cross = s("line", { class: "crosshair", y1: pad.t, y2: H - pad.b, style: "display:none" });
  svg.append(cross, hit);
  hit.addEventListener("mousemove", (ev) => {
    const box = svg.getBoundingClientRect();
    const px = ((ev.clientX - box.left) / box.width) * W;
    const idx = Math.max(0, Math.min(moves.length - 1,
      Math.round(((px - pad.l) / (W - pad.l - pad.r)) * (moves.length - 1))));
    cross.setAttribute("x1", X(idx)); cross.setAttribute("x2", X(idx));
    cross.style.display = "";
    onPick(idx, true);
  });
  hit.addEventListener("click", (ev) => {
    const box = svg.getBoundingClientRect();
    const px = ((ev.clientX - box.left) / box.width) * W;
    const idx = Math.max(0, Math.min(moves.length - 1,
      Math.round(((px - pad.l) / (W - pad.l - pad.r)) * (moves.length - 1))));
    onPick(idx, false);
  });
  wrap.append(svg);
  return wrap;
}

async function openGame(uuid) {
  const dlg = $("#dlg-game");
  $("#g-body").replaceChildren(h("div", { class: "empty" }, h("span", { class: "spinner" })));
  dlg.showModal();
  let data;
  try {
    data = await api("/api/game", { uuid });
  } catch (err) {
    $("#g-body").replaceChildren(h("div", { class: "empty" }, err.message));
    return;
  }
  const g = data.game, moves = data.moves;
  $("#g-title").textContent =
    `${g.white_username || "?"} vs ${g.black_username || "?"}`;
  $("#g-sub").textContent =
    `${date(g.played_at)} · ${g.time_class || ""} ${g.time_control || ""} · you played ` +
    `${g.color}, ${g.result}${g.termination ? " by " + g.termination : ""}`;

  const body = $("#g-body");
  body.replaceChildren();

  if (!moves.length) {
    add(body, h("div", { class: "empty" },
      "This game has not been analyzed yet. Run Analyze to fill it in."),
      g.url ? h("div", { style: "text-align:center" }, h("a", { href: g.url, target: "_blank" }, "Open on Chess.com")) : null);
    return;
  }

  const boardWrap = h("div", {});
  const caption = h("div", { class: "movecaption" });
  let selected = moves.length - 1;

  const counter = h("span", { class: "counter" });
  const btn = (label, title, target) =>
    h("button", { class: "icon", title, onclick: () => show(target()) }, label);
  const btnFirst = btn("⏮", "First move (Home)", () => 0);
  const btnPrev = btn("◀", "Previous move (left arrow)", () => selected - 1);
  const btnNext = btn("▶", "Next move (right arrow)", () => selected + 1);
  const btnLast = btn("⏭", "Last move (End)", () => moves.length - 1);
  const nav = h("div", { class: "boardnav" },
    btnFirst, btnPrev, counter, btnNext, btnLast);

  function show(idx) {
    idx = Math.max(0, Math.min(moves.length - 1, idx));
    selected = idx;
    const m = moves[idx];
    const showBest = m.is_player && ["blunder", "mistake", "inaccuracy"].includes(m.judgment);
    boardWrap.replaceChildren(board(m.fen_before, {
      flip: g.color === "black",
      highlight: squaresOf(m.uci),
      highlight2: showBest ? squaresOf(m.best_uci) : [],
      arrows: moveArrows(m.uci, showBest ? m.best_uci : null),
    }));
    const dots = m.side === "black" ? "..." : ".";
    fill(caption,
      h("div", {}, h("b", {}, `${m.move_no}${dots} ${m.san}`),
        m.special
          ? h("span", { class: "tag " + m.special, style: "margin-left:8px" },
              m.special)
          : m.is_player && m.judgment !== "best" && m.judgment !== "good"
          ? h("span", { class: "tag", style: "margin-left:8px" }, m.judgment) : null),
      m.is_player && m.best_san && m.best_san !== m.san && ["blunder", "mistake", "inaccuracy"].includes(m.judgment)
        ? h("div", { style: "color:var(--text-secondary)" },
            `engine preferred ${m.best_san}, this cost ${num(m.winp_loss, 0)}% win probability`)
        : null,
      m.clock_secs ? h("div", { style: "color:var(--text-muted);font-size:12px" },
        `clock ${Math.floor(m.clock_secs / 60)}:${String(Math.floor(m.clock_secs % 60)).padStart(2, "0")}`) : null);
    counter.textContent = `${idx + 1} / ${moves.length}`;
    btnFirst.disabled = btnPrev.disabled = idx === 0;
    btnNext.disabled = btnLast.disabled = idx === moves.length - 1;
    for (const node of body.querySelectorAll(".movelist .mv")) {
      const on = Number(node.dataset.idx) === idx;
      node.classList.toggle("sel", on);
      if (on) keepInView(node.closest(".movelist"), node);
    }
    if (aiMode === "move") renderAI();
  }

  // Arrow keys are how anyone who has used a board site expects to step through
  // a game. Replaced rather than stacked, so reopening the dialog cannot leave
  // two handlers driving the same board.
  if (gameKeys) $("#dlg-game").removeEventListener("keydown", gameKeys);
  gameKeys = (ev) => {
    const jump = { ArrowLeft: -1, ArrowRight: 1 }[ev.key];
    if (jump) show(selected + jump);
    else if (ev.key === "Home") show(0);
    else if (ev.key === "End") show(moves.length - 1);
    else return;
    ev.preventDefault();
  };
  $("#dlg-game").addEventListener("keydown", gameKeys);

  // Columns are White and Black, not you and them, so say which one is yours.
  const list = h("div", { class: "movelist" });
  const you = (side) => (g.color === side ? " (you)" : "");
  list.append(
    h("div", { class: "colhead" }, ""),
    h("div", { class: "colhead" + (g.color === "white" ? " mine" : "") }, "White" + you("white")),
    h("div", { class: "colhead" + (g.color === "black" ? " mine" : "") }, "Black" + you("black")));
  for (let i = 0; i < moves.length; i += 2) {
    list.append(h("div", { class: "no" }, moves[i].move_no + "."));
    for (const m of [moves[i], moves[i + 1]]) {
      if (!m) { list.append(h("div", {})); continue; }
      // A special move outranks the quality mark: it is the more interesting
      // fact, and a brilliant move is never also a blunder.
      const mark = { brilliant: "!!", great: "!" }[m.special]
        || { blunder: "??", mistake: "?", inaccuracy: "?!" }[m.judgment] || "";
      // Always from YOUR point of view: your errors read negative, your
      // opponent's errors read positive, because their loss is your gain.
      // Threshold keeps the list quiet — most moves move the needle by 0-1.
      const swing = m.winp_loss >= 5 ? Math.round(m.winp_loss) : 0;
      const delta = m.is_player ? -swing : swing;
      const idx = m.ply - 1;
      list.append(h("div", {
        class: "mv " + (m.special ? m.special : m.is_player ? m.judgment : "opp"),
        title: (m.is_player ? "your move" : "opponent's move") +
          (m.special === "brilliant"
            ? " — brilliant: a sound sacrifice"
            : m.special === "great"
            ? ` — great: the only move (the alternative would have cost ${Math.round(m.alt_winp_gap || 0)}%)`
            : m.judgment && m.judgment !== "best" && m.judgment !== "good"
            ? ` (${m.judgment}, ${Math.round(m.winp_loss)} points of win probability)`
            : ""),
        dataset: { idx },
        onclick: () => show(idx),
      }, m.san + mark, delta
        ? h("span", { class: "drop" + (delta > 0 ? " gain" : "") },
            `(${delta > 0 ? "+" : ""}${delta})`)
        : null));
    }
  }

  // One AI panel for both threads. Previously the move chat lived under the
  // board and the game chat in the right column, which meant two conversations
  // competing for attention in two different places.
  let aiMode = "game";
  const cache = {};
  const chats = {};

  function chatFor(ply) {
    const key = ply === null ? "game" : `move:${ply}`;
    if (!chats[key]) {
      chats[key] = chatThread(uuid, ply,
        ply === null ? "Ask a follow-up about this game…"
                     : "Ask a follow-up about this move…");
      chats[key].load();
    }
    return chats[key];
  }

  const aiBody = h("div", { class: "aibody" });
  const aiFoot = h("div", { class: "aifoot" });
  const tabGame = h("button", { onclick: () => { aiMode = "game"; renderAI(); } },
    "This game");
  const tabMove = h("button", { onclick: () => { aiMode = "move"; renderAI(); } },
    "This move");
  const aiPanel = h("div", { class: "aipanel" },
    h("div", { class: "aitabs" }, tabGame, tabMove), aiBody, aiFoot);

  function busyPanel(label) {
    fill(aiBody, h("div", { class: "empty" }, h("span", { class: "spinner" }),
      " ", label));
    aiFoot.replaceChildren();
  }

  async function runExplain(refresh) {
    const idx = selected;
    const ply = moves[idx].ply;
    busyPanel(`thinking about ${moves[idx].san}…`);
    try {
      const res = await post("/api/explain", { uuid, ply, refresh: !!refresh });
      cache[`move:${ply}`] = res;
      if (selected === idx && aiMode === "move") renderAI();
    } catch (err) {
      if (selected === idx && aiMode === "move") {
        fill(aiBody, h("div", { class: "explain-err" }, err.message));
        aiFoot.replaceChildren();
      }
    }
  }

  async function runReview(refresh) {
    busyPanel("reviewing the game…");
    try {
      cache.game = await post("/api/explain", { uuid, refresh: !!refresh });
      if (aiMode === "game") renderAI();
    } catch (err) {
      if (aiMode === "game") {
        fill(aiBody, h("div", { class: "explain-err" }, err.message));
        aiFoot.replaceChildren();
      }
    }
  }

  async function showRequest() {
    busyPanel("building the request…");
    try {
      const body = aiMode === "game" ? { uuid } : { uuid, ply: moves[selected].ply };
      const res = await post("/api/explain-preview", body);
      fill(aiBody,
        h("div", { class: "explain-meta" }, `provider: ${res.provider}`),
        h("h4", { class: "previewhead" }, "system prompt"),
        h("pre", { class: "preview" }, res.system),
        h("h4", { class: "previewhead" }, "user message"),
        h("pre", { class: "preview" }, res.user),
        h("div", { class: "explainrow" },
          h("button", { onclick: () => renderAI() }, "Back")));
    } catch (err) {
      fill(aiBody, h("div", { class: "explain-err" }, err.message));
    }
    aiFoot.replaceChildren();
  }

  function renderAI() {
    tabMove.setAttribute("aria-selected", String(aiMode === "move"));
    tabGame.setAttribute("aria-selected", String(aiMode === "game"));

    const isMove = aiMode === "move";
    const m = moves[selected];
    const key = isMove ? `move:${m.ply}` : "game";
    const hit = cache[key];
    const dots = m.side === "black" ? "..." : ".";

    const actions = h("div", { class: "explainrow" },
      h("button", { class: hit ? null : "primary",
        onclick: () => (isMove ? runExplain(hit ? true : false) : runReview(!!hit)) },
        hit ? "Ask again" : (isMove ? `Explain ${m.move_no}${dots} ${m.san}`
                                    : "Review this game")),
      h("button", { title: "See the exact prompt and data that would be sent",
        onclick: showRequest }, "Show request"));

    if (!hit) {
      fill(aiBody, actions, h("div", { class: "empty" }, isMove
        ? "No explanation for this move yet."
        : "No review of this game yet."));
      aiFoot.replaceChildren();
      return;
    }
    const thread = chatFor(isMove ? m.ply : null);
    fill(aiBody, actions,
      h("p", { class: "explain-text" }, hit.text),
      h("div", { class: "explain-meta" },
        hit.model ? `${hit.model}${hit.cached ? ", cached" : ""}` : "cached"),
      thread.log);
    fill(aiFoot, thread.row);
  }

  // The board sits in its own band across the top so it can be read at a
  // useful size; the three columns below carry everything else.
  const topBand = h("div", { class: "gametop" },
    h("div", { class: "boardcol" }, boardWrap, nav, caption));

  const colChart = h("div", { class: "col-chart" },
    h("h3", { class: "colhead" }, "Win probability, your side"),
    evalChart(moves, g.color, (idx) => show(idx)),
    h("div", { class: "gamestats" },
      h("span", {}, `your ACPL ${int(g.my_acpl)} (opponent ${int(g.opp_acpl)})`),
      h("span", {}, `accuracy ${num(g.my_accuracy)}% (opponent ${num(g.opp_accuracy)}%)`),
      g.url ? h("a", { href: g.url, target: "_blank" }, "on Chess.com") : null));

  const colMoves = h("div", { class: "col-moves" },
    h("h3", { class: "colhead" }, "Moves"), list);

  add(body,
    h("div", { class: "gamebody" }, topBand, colChart, colMoves, aiPanel),
    symbolKey());

  // Open on your worst move if there is one, otherwise the final position.
  const firstBlunder = moves.findIndex((m) => m.is_player && m.judgment === "blunder");
  show(firstBlunder === -1 ? moves.length - 1 : firstBlunder);
  renderAI();
}

/* ------------------------------------------------------------ jobs polling */

let pollTimer = null;

async function pollJobs() {
  let data;
  try {
    data = await api("/api/jobs", { limit: 5 });
  } catch (err) {
    return;
  }
  const job = data.jobs[0];
  const dot = $("#job-dot"), text = $("#job-text"), fill = $("#job-fill");
  const running = job && job.status === "running";

  dot.className = "dot " + (running ? "running" : job ? job.status : "");
  $("#btn-cancel").hidden = !running;
  $("#btn-sync").disabled = running;
  $("#btn-analyze").disabled = running;

  if (!job) {
    text.textContent = "idle, nothing has run yet";
    fill.style.width = "0%";
  } else if (running) {
    text.textContent = `${job.kind}: ${job.message || "working"}`;
    const share = job.progress_total ? (job.progress_done / job.progress_total) * 100 : 8;
    fill.style.width = Math.max(4, share) + "%";
  } else {
    const label = job.status === "failed" ? `failed: ${job.error}` : job.message || job.status;
    text.textContent = `last ${job.kind} (${job.trigger}) ${ago(job.finished_at)}: ${label}`;
    fill.style.width = job.status === "done" ? "100%" : "0%";
  }
  $("#job-next").textContent = data.next_run_at && !running
    ? "next auto sync " + until(data.next_run_at) : "";

  if (running && !pollJobs.wasRunning) pollJobs.wasRunning = true;
  if (!running && pollJobs.wasRunning) {
    pollJobs.wasRunning = false;
    await loadConfig();
    await render();
  }
  clearTimeout(pollTimer);
  pollTimer = setTimeout(pollJobs, running ? 1500 : 10000);
}

/* --------------------------------------------------------------- settings */

async function loadConfig() {
  state.config = await api("/api/config");
  const c = state.config;
  $("#who").textContent = c.resolved_player ? `@${c.resolved_player}` : "no username set";
  const cov = c.coverage || {};
  $("#coverage").replaceChildren(
    h("span", { class: "pill" }, "games ", h("b", {}, int(cov.games || 0))),
    h("span", { class: "pill" }, "analyzed ", h("b", {}, int(cov.analyzed || 0))),
    h("span", { class: "pill" }, "pending ", h("b", {}, int(cov.pending || 0))),
    h("span", { class: "pill" }, "last game ", h("b", {}, cov.last_played ? date(cov.last_played) : "-")));
}

/* Editors are built from whatever the API reports, so a new prompt kind needs
   no UI change. */
const PROMPT_LABELS = {
  move: "Single move",
  game: "Whole game",
  game_clean: "Whole game, when you made no mistakes",
};

async function loadPrompts() {
  const list = $("#p-list");
  try {
    const res = await api("/api/prompts");
    $("#p-preamble").textContent = res.user_preamble;
    fill(list, Object.entries(res.prompts).map(([kind, info]) => {
      const area = h("textarea", {
        rows: kind === "move" ? 10 : 8, spellcheck: "false",
        style: "width:100%;font-family:ui-monospace,monospace;font-size:12px",
      });
      area.value = info.text;
      const state = h("span", {
        style: "font-size:12px;color:var(--text-muted);align-self:center",
      }, info.is_default ? "using the default" : "edited");
      const save = async (text) => {
        try {
          const out = await post("/api/prompts", { kind, text });
          area.value = out.prompts[kind].text;
          state.textContent = out.prompts[kind].is_default
            ? "using the default" : "edited";
          toast(text ? `Saved ${kind} prompt` : `Reset ${kind} prompt`);
        } catch (err) { toast(err.message, true); }
      };
      return h("div", { style: "margin-bottom:16px" },
        h("label", { class: "field" }, PROMPT_LABELS[kind] || kind, area),
        h("div", { class: "explainrow" },
          h("button", { class: "primary", onclick: () => save(area.value) },
            "Save"),
          // An empty string is the reset signal the API expects.
          h("button", { onclick: () => save("") }, "Reset to default"),
          state));
    }));
  } catch (err) {
    fill(list, h("div", { class: "explain-err" }, err.message));
  }
}

function openSettings() {
  const s = state.config.settings;
  $("#s-player").value = s.player || "";
  $("#s-interval").value = s.interval_minutes;
  $("#s-depth").value = s.depth;
  $("#s-threads").value = s.threads;
  $("#s-hash").value = s.hash_mb;
  $("#s-batch").value = s.batch_size;
  $("#s-auto").checked = !!s.auto_sync;
  $("#s-llm-provider").value = s.llm_provider || "";
  $("#s-llm-model").value = s.llm_model || "";
  $("#s-llm-base-url").value = s.llm_base_url || "";
  $("#s-llm-api-key").value = "";
  $("#s-llm-keystate").textContent = s.llm_api_key_set
    ? "a key is saved" : "no key saved";
  $("#s-llm-result").replaceChildren();
  $("#dlg-settings").showModal();
  loadPrompts();
}

async function saveSettings() {
  try {
    state.config = await post("/api/config", {
      player: $("#s-player").value.trim(),
      interval_minutes: Number($("#s-interval").value),
      depth: Number($("#s-depth").value),
      threads: Number($("#s-threads").value),
      hash_mb: Number($("#s-hash").value),
      batch_size: Number($("#s-batch").value),
      auto_sync: $("#s-auto").checked,
      llm_provider: $("#s-llm-provider").value,
      llm_model: $("#s-llm-model").value.trim(),
      llm_base_url: $("#s-llm-base-url").value.trim(),
      ...(($("#s-llm-api-key").value.trim())
        ? { llm_api_key: $("#s-llm-api-key").value.trim() } : {}),
    });
    $("#dlg-settings").close();
    await loadConfig();
    await render();
    toast("Settings saved");
  } catch (err) {
    toast(err.message, true);
  }
}

/* ------------------------------------------------------------------- init */

/* One absent element used to throw here and silently kill every listener
   registered after it — including the dialog close buttons. */
function on(sel, event, handler) {
  const node = $(sel);
  if (!node) {
    console.warn(`wire: ${sel} not found, listener skipped`);
    return;
  }
  node.addEventListener(event, handler);
}

function wire() {
  // Bound first: closing a dialog must survive any later wiring failure.
  for (const btn of document.querySelectorAll("[data-close]")) {
    btn.addEventListener("click", () => btn.closest("dialog").close());
  }

  on("#tabs", "click", (ev) => {
    const btn = ev.target.closest("button[data-tab]");
    if (!btn) return;
    state.tab = btn.dataset.tab;
    for (const b of $("#tabs").querySelectorAll("button")) {
      b.setAttribute("aria-selected", String(b === btn));
    }
    render();
  });

  const bind = (sel, key, prop = "value") => {
    $(sel).addEventListener("change", () => {
      state.filters[key] = prop === "checked" ? $(sel).checked : $(sel).value;
      render();
    });
  };
  bind("#f-time-class", "time_class");
  bind("#f-color", "color");
  bind("#f-since", "since");
  bind("#f-until", "until");
  bind("#f-rated", "rated_only", "checked");
  bind("#f-min-games", "min_games");
  on("#f-reset", "click", () => {
    state.filters = { time_class: "", color: "", since: "", until: "", rated_only: false, min_games: 3 };
    $("#f-time-class").value = ""; $("#f-color").value = "";
    $("#f-since").value = ""; $("#f-until").value = "";
    $("#f-rated").checked = false; $("#f-min-games").value = 3;
    render();
  });

  const job = async (kind) => {
    try {
      await post("/api/jobs", { kind });
      pollJobs.wasRunning = true;
      toast(kind === "sync" ? "Sync queued" : "Analysis queued");
      pollJobs();
    } catch (err) { toast(err.message, true); }
  };
  on("#btn-sync", "click", () => job("sync"));
  on("#btn-analyze", "click", () => job("analyze"));
  on("#btn-cancel", "click", async () => {
    await post("/api/jobs/cancel");
    toast("Stopping after the current game");
    pollJobs();
  });

  on("#btn-settings", "click", openSettings);
  on("#s-save", "click", saveSettings);

  on("#s-reanalyze", "click", async () => {
    if (!confirm("Re-analyze every game from scratch? This runs in the "
        + "background and can take a while, but you can keep using the app.")) {
      return;
    }
    try {
      await post("/api/jobs", { kind: "analyze", reanalyze: true });
      pollJobs.wasRunning = true;
      toast("Re-analysis queued");
      pollJobs();
      $("#dlg-settings").close();
    } catch (err) { toast(err.message, true); }
  });

  on("#s-llm-clear-key", "click", async () => {
    try {
      state.config = await post("/api/config", { llm_api_key: "" });
      $("#s-llm-api-key").value = "";
      $("#s-llm-keystate").textContent = "no key saved";
      toast("Saved key cleared");
    } catch (err) { toast(err.message, true); }
  });

  on("#s-llm-test", "click", async () => {
    const out = $("#s-llm-result");
    fill(out, h("div", { class: "empty" }, h("span", { class: "spinner" })));
    try {
      const res = await post("/api/llm-test", {
        base_url: $("#s-llm-base-url").value.trim(),
        api_key: $("#s-llm-api-key").value.trim(),
      });
      const rows = res.tried.map((t) =>
        h("div", {},
          h("code", {}, t.url), " → ",
          h("b", { style: t.status === 200 ? "color:var(--good)" : "color:var(--critical)" },
            t.status === null ? "unreachable" : String(t.status)),
          t.note ? ` (${t.note})` : ""));
      fill(out,
        h("div", {}, ...rows),
        res.working_base
          ? h("div", { style: "margin-top:8px;color:var(--good)" },
              "Use this as the URL: ", h("code", {}, res.working_base))
          : h("div", { style: "margin-top:8px;color:var(--critical)" },
              "Nothing answered. Check the server is running and the port is right."),
        res.models && res.models.length
          ? h("div", { class: "explain-meta" }, "models: " + res.models.join(", "))
          : null);
    } catch (err) {
      fill(out, h("div", { class: "explain-err" }, err.message));
    }
  });
  on("#s-import", "click", async () => {
    const pgn = $("#s-pgn").value;
    if (!pgn.trim()) return;
    try {
      const res = await post("/api/import", { pgn });
      $("#s-pgn").value = "";
      toast(`Imported ${res.imported}, skipped ${res.skipped}`);
      await loadConfig();
      await render();
    } catch (err) { toast(err.message, true); }
  });
  on("#btn-theme", "click", () => {
    const now = document.documentElement.getAttribute("data-theme");
    const next = now === "dark" ? "light" : now === "light" ? "" : "dark";
    if (next) document.documentElement.setAttribute("data-theme", next);
    else document.documentElement.removeAttribute("data-theme");
    localStorage.setItem("theme", next);
  });
  const saved = localStorage.getItem("theme");
  if (saved) document.documentElement.setAttribute("data-theme", saved);
}

(async function start() {
  wire();
  try {
    await loadConfig();
  } catch (err) {
    toast("Could not reach the server: " + err.message, true);
  }
  await render();
  pollJobs();
})();
