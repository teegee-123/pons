// pons paper trader dashboard: core helpers, header, leaderboard, strategy detail/editor.
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const api = async (path, body) => {
  const r = await fetch(path, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) throw new Error(path + " " + r.status);
  return r.json();
};
const S = { state: null, meta: null, tab: "board", kind: "all", sort: "score", sel: null, editing: false, h: 1 };

// ---------- formatting ----------
const fNum = (x, d = 2) => x == null ? "–" : Number(x).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
const fUsd = (x, d = 2) => x == null ? "–" : (x < 0 ? "−$" : "$") + fNum(Math.abs(x), d);
const fSUsd = (x, d = 2) => x == null ? "–" : (x > 0 ? "+" : x < 0 ? "−" : "") + "$" + fNum(Math.abs(x), d);
const fPct = (x, d = 1, signed = true) => x == null ? "–" : (signed && x > 0 ? "+" : x < 0 ? "−" : "") + fNum(Math.abs(x) * 100, d) + "%";
const fP = (x, d = 1) => x == null ? "–" : (x > 0 ? "+" : x < 0 ? "−" : "") + fNum(Math.abs(x), d) + "%"; // already in %
const cls = x => x == null || Math.abs(x) < 1e-12 ? "dim" : x > 0 ? "pos" : "neg";
const fK = x => x == null ? "–" : x >= 1e6 ? "$" + fNum(x / 1e6, 2) + "M" : x >= 1e3 ? "$" + fNum(x / 1e3, 1) + "k" : "$" + fNum(x, 0);
const fDur = s => s == null ? "–" : s < 90 ? Math.round(s) + "s" : s < 5400 ? Math.round(s / 60) + "m" : s < 172800 ? fNum(s / 3600, 1) + "h" : fNum(s / 86400, 1) + "d";
const fMin = m => m == null ? "–" : fDur(m * 60);
const fTime = t => t ? new Date(t * 1000).toLocaleTimeString() : "–";
const toast = msg => { const t = $("#toast"); t.textContent = msg; t.style.display = "block"; clearTimeout(t._h); t._h = setTimeout(() => t.style.display = "none", 2200); };

// ---------- tooltip ----------
const tip = $("#tip");
function showTip(e, html) { tip.innerHTML = html; tip.style.display = "block"; moveTip(e); }
function moveTip(e) {
  const w = tip.offsetWidth, h = tip.offsetHeight;
  let x = e.clientX + 14, y = e.clientY + 14;
  if (x + w > innerWidth - 8) x = e.clientX - w - 14;
  if (y + h > innerHeight - 8) y = e.clientY - h - 14;
  tip.style.left = x + "px"; tip.style.top = y + "px";
}
const hideTip = () => tip.style.display = "none";
document.addEventListener("mouseover", e => { const el = e.target.closest("[data-tip]"); if (el) showTip(e, el.dataset.tip); });
document.addEventListener("mousemove", e => { if (tip.style.display === "block" && e.target.closest("[data-tip]")) moveTip(e); });
document.addEventListener("mouseout", e => { if (e.target.closest("[data-tip]")) hideTip(); });

// ---------- charts ----------
function sparkline(vals, base, w = 120, h = 28) {
  if (!vals || vals.length < 2) return `<svg width="${w}" height="${h}"></svg>`;
  const lo = Math.min(...vals, base), hi = Math.max(...vals, base), span = hi - lo || 1;
  const X = i => (i / (vals.length - 1)) * (w - 4) + 2, Y = v => h - 3 - ((v - lo) / span) * (h - 6);
  const d = vals.map((v, i) => (i ? "L" : "M") + X(i).toFixed(1) + " " + Y(v).toFixed(1)).join("");
  return `<svg width="${w}" height="${h}" aria-hidden="true"><line x1="2" x2="${w - 2}" y1="${Y(base)}" y2="${Y(base)}" stroke="var(--border)" stroke-dasharray="2 3"/>` +
    `<path d="${d}" fill="none" stroke="var(--line)" stroke-width="1.5" stroke-linejoin="round"/></svg>`;
}

function equityChart(el, pts, base) {
  if (!pts || pts.length < 2) { el.innerHTML = `<div class="empty">Equity curve appears after a couple of minutes.</div>`; return; }
  const W = Math.max(320, el.clientWidth || 600), H = 180, L = 56, R = 10, T = 10, B = 22;
  const vs = pts.map(p => p[1]), ts = pts.map(p => p[0]);
  const lo = Math.min(...vs, base), hi = Math.max(...vs, base), pad = (hi - lo) * 0.08 || 1;
  const y0 = lo - pad, y1 = hi + pad, t0 = ts[0], t1 = ts[ts.length - 1] || t0 + 1;
  const X = t => L + ((t - t0) / (t1 - t0 || 1)) * (W - L - R), Y = v => T + (1 - (v - y0) / (y1 - y0)) * (H - T - B);
  const ticks = [y0 + (y1 - y0) * 0.1, (y0 + y1) / 2, y1 - (y1 - y0) * 0.1];
  const d = pts.map((p, i) => (i ? "L" : "M") + X(p[0]).toFixed(1) + " " + Y(p[1]).toFixed(1)).join("");
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="Equity over time">
    ${ticks.map(v => `<line x1="${L}" x2="${W - R}" y1="${Y(v)}" y2="${Y(v)}" stroke="var(--border)"/><text x="${L - 6}" y="${Y(v) + 4}" text-anchor="end" font-size="10.5" fill="var(--muted)">${fUsd(v, 0)}</text>`).join("")}
    <line x1="${L}" x2="${W - R}" y1="${Y(base)}" y2="${Y(base)}" stroke="var(--muted)" stroke-dasharray="3 3"/>
    <text x="${L}" y="${H - 6}" font-size="10.5" fill="var(--muted)">${fTime(t0)}</text>
    <text x="${W - R}" y="${H - 6}" font-size="10.5" fill="var(--muted)" text-anchor="end">${fTime(t1)}</text>
    <path d="${d}" fill="none" stroke="var(--line)" stroke-width="2" stroke-linejoin="round"/>
    <line class="xh" y1="${T}" y2="${H - B}" stroke="var(--muted)" visibility="hidden"/>
    <circle class="xd" r="4" fill="var(--line)" stroke="var(--surface)" stroke-width="2" visibility="hidden"/>
    <rect x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}" fill="transparent"/></svg>`;
  const svg = $("svg", el), xh = $(".xh", svg), xd = $(".xd", svg);
  svg.addEventListener("mousemove", e => {
    const r = svg.getBoundingClientRect(), tx = t0 + ((e.clientX - r.left) * (W / r.width) - L) / (W - L - R) * (t1 - t0);
    let best = 0; ts.forEach((t, i) => { if (Math.abs(t - tx) < Math.abs(ts[best] - tx)) best = i; });
    const x = X(ts[best]), y = Y(vs[best]);
    xh.setAttribute("x1", x); xh.setAttribute("x2", x); xh.setAttribute("visibility", "visible");
    xd.setAttribute("cx", x); xd.setAttribute("cy", y); xd.setAttribute("visibility", "visible");
    showTip(e, `<b>${fUsd(vs[best])}</b> <span class="${cls(vs[best] - base)}">${fSUsd(vs[best] - base)}</span><br><span class="dim">${new Date(ts[best] * 1000).toLocaleString()}</span>`);
  });
  svg.addEventListener("mouseleave", () => { xh.setAttribute("visibility", "hidden"); xd.setAttribute("visibility", "hidden"); hideTip(); });
}

// ---------- header ----------
function renderHeader() {
  const st = S.state.status, t = S.state.totals, ev = S.state.evo;
  const age = st.lastPollAt ? S.state.now - st.lastPollAt : null;
  const ok = age != null && age < 15;
  $("#liveDot").className = "dot " + (ok ? "ok" : "bad");
  $("#liveTxt").textContent = ok ? `live · poll ${st.lastPollMs}ms` : (st.lastError ? "poll error" : "waiting for data…");
  $("#liveTxt").parentElement.dataset.tip = esc(st.lastError || "") + `<br>polls ${st.polls}, errors ${st.pollErrors}, coverage gaps ${st.gaps}, direct refreshes ${st.refreshes}<br>chain quote calls ${st.chainCalls} (failed ${st.chainFailures})<br>storage: ${esc(st.storage)}` +
    (st.storageError ? " — " + esc(st.storageError) : "") + (st.executorError ? "<br>" + esc(st.executorError) : "");
  const k = (label, val, c = "") => `<div class="kpi"><b class="${c}">${val}</b><span>${label}</span></div>`;
  $("#kpis").innerHTML = k("manual strategies P&L", fSUsd(t.manualPnl), cls(t.manualPnl)) + k("all strategies P&L", fSUsd(t.allPnl), cls(t.allPnl)) +
    k("closed trades", fNum(t.trades, 0)) + k("open positions", fNum(t.open, 0)) + k("tokens tracked", fNum(st.tokens, 0)) +
    k("ETH", fUsd(st.ethUsd, 0)) + k(`epoch ${ev.epoch} · next evolve`, S.state.cfg.evolution.enabled ? fDur(ev.nextEpochIn) : "off");
}

// ---------- leaderboard ----------
const COLS = [
  ["name", "Strategy"], ["score", "Score", "n"], ["trades", "Trades", "n"], ["open", "Open", "n"], ["winRate", "Win", "n"],
  ["avgRet", "Avg/trade", "n"], ["pnl", "P&L", "n"], ["curve", "Equity"], ["maxDD", "Max DD", "n"], ["profitFactor", "PF", "n"],
  ["avgHoldMin", "Avg hold", "n"], ["fees", "Fees+gas", "n"], ["missed", "Reverts", "n"],
];
function renderBoard() {
  let rows = S.state.rows.filter(r => S.kind === "all" || r.kind === S.kind);
  const key = S.sort;
  rows = [...rows].sort((a, b) => key === "name" ? a.name.localeCompare(b.name) : ((b[key] ?? -Infinity) - (a[key] ?? -Infinity)));
  $("#boardSub").textContent = `${S.state.totals.strategies} running · sorted by ${COLS.find(c => c[0] === key)?.[1] || key}`;
  const head = "<tr>" + COLS.map(([k, l, n]) => `<th class="${n || ""} ${k !== "curve" ? "sortable" : ""} ${k === key ? "sorted" : ""}" data-k="${k}">${l}</th>`).join("") + "</tr>";
  const body = rows.map(r => `<tr class="click ${r.id === S.sel ? "sel" : ""}" data-id="${r.id}">
    <td><span class="tag ${r.kind}">${r.kind}${r.gen ? " g" + r.gen : ""}</span><b>${esc(r.name)}</b>${r.enabled ? "" : ' <span class="tag">paused</span>'}<div class="desc">${esc(r.desc)}</div></td>
    <td class="n ${cls(r.score)}">${fP(r.score, 2)}</td><td class="n">${r.trades}</td><td class="n">${r.open}${r.pending ? `<span class="dim">+${r.pending}</span>` : ""}</td>
    <td class="n">${r.winRate == null ? "–" : fNum(r.winRate * 100, 0) + "%"}</td><td class="n ${cls(r.avgRet)}">${fPct(r.avgRet)}</td>
    <td class="n ${cls(r.pnl)}" data-tip="realized ${fSUsd(r.realized)}<br>unrealized ${fSUsd(r.unrealized)}<br>equity ${fUsd(r.equity)} of ${fUsd(r.bankroll, 0)}">${fSUsd(r.pnl)}</td>
    <td>${sparkline(r.curve, r.bankroll)}</td><td class="n">${fPct(r.maxDD, 1, false)}</td>
    <td class="n">${r.profitFactor == null ? "–" : r.profitFactor >= 999 ? "∞" : fNum(r.profitFactor, 2)}</td>
    <td class="n">${fMin(r.avgHoldMin)}</td><td class="n">${fUsd((r.fees || 0) + (r.gas || 0))}</td><td class="n">${r.missed + r.sellFails || "–"}</td></tr>`).join("");
  $("#boardTbl").innerHTML = head + (body || `<tr><td colspan="13" class="empty">No strategies.</td></tr>`);
  const ev = S.state.evo;
  $("#hallTbl").innerHTML = "<tr><th>Strategy</th><th class='n'>Score</th><th class='n'>Trades</th><th class='n'>P&L</th><th></th></tr>" +
    (ev.hall.map(h => `<tr><td><b>${esc(h.name)}</b>${h.alive ? "" : ' <span class="tag">retired</span>'}<div class="desc">${esc(h.desc)}</div></td>
      <td class="n ${cls(h.score)}">${fP(h.score, 2)}</td><td class="n">${h.summary.trades}</td><td class="n ${cls(h.summary.pnl)}">${fSUsd(h.summary.pnl)}</td>
      <td><button class="btn" data-revive="${h.id}" title="Add a copy as a manual strategy">Revive</button></td></tr>`).join("") || `<tr><td colspan="5" class="empty">Filled at the first evolution epoch.</td></tr>`);
  $("#retiredTbl").innerHTML = "<tr><th>Strategy</th><th class='n'>Score</th><th class='n'>Trades</th><th class='n'>P&L</th><th>Why</th></tr>" +
    (ev.retired.map(h => `<tr><td><b>${esc(h.name)}</b><div class="desc">${esc(h.desc)}</div></td><td class="n ${cls(h.score)}">${fP(h.score, 2)}</td>
      <td class="n">${h.summary.trades}</td><td class="n ${cls(h.summary.pnl)}">${fSUsd(h.summary.pnl)}</td><td class="dim">${esc(h.why)}</td></tr>`).join("") || `<tr><td colspan="5" class="empty">Nothing retired yet.</td></tr>`);
}
$("#boardTbl").addEventListener("click", e => {
  const th = e.target.closest("th.sortable"); if (th) { S.sort = th.dataset.k; renderBoard(); return; }
  const tr = e.target.closest("tr[data-id]"); if (tr) openDetail(tr.dataset.id);
});
$("#kindChips").addEventListener("click", e => { const b = e.target.closest("button"); if (!b) return; S.kind = b.dataset.k; $$("#kindChips button").forEach(x => x.classList.toggle("on", x === b)); renderBoard(); });
$("#hallTbl").addEventListener("click", async e => { const b = e.target.closest("[data-revive]"); if (!b) return; await api(`/api/strategy/${b.dataset.revive}/action`, { action: "revive" }); toast("Revived as a manual strategy"); refresh(); });
$("#btnEvolve").onclick = async () => { await api("/api/evolve", {}); toast("Evolution epoch run"); refresh(); };
$("#btnNew").onclick = () => openEditor(null);

// ---------- strategy detail ----------
async function openDetail(id) {
  S.sel = id; S.editing = false;
  $("#boardSplit").classList.add("detail-open"); $("#detail").classList.remove("hidden");
  await loadDetail(); renderBoard();
  if (innerWidth < 1200) $("#detail").scrollIntoView({ behavior: "smooth" });
}
function closeDetail() { S.sel = null; S.editing = false; $("#detail").classList.add("hidden"); $("#boardSplit").classList.remove("detail-open"); renderBoard(); }

async function loadDetail() {
  if (!S.sel || S.editing) return;
  let v; try { v = await api("/api/strategy/" + S.sel); } catch { closeDetail(); return; }
  if (S.editing) return;
  const r = v.row, sp = v.spec, el = $("#detail");
  const stat = (l, x, c = "") => `<div class="stat"><span>${l}</span><b class="${c}">${x}</b></div>`;
  el.innerHTML = `<div class="row"><h3>${esc(sp.name)}</h3><span class="tag ${sp.kind}">${sp.kind}${sp.gen ? " gen " + sp.gen : ""}</span><div class="spacer"></div><button class="btn" data-a="close">✕</button></div>
    <div class="desc" style="margin-top:4px">${esc(r.desc)}</div>
    <div class="row" style="margin-top:10px">
      <button class="btn" data-a="edit">Edit filters</button>
      ${sp.kind === "auto" ? '<button class="btn primary" data-a="promote" title="Keep it: never retired by evolution">Promote to manual</button>' : ""}
      <button class="btn" data-a="clone">Clone</button>
      <button class="btn" data-a="${sp.enabled ? "pause" : "resume"}">${sp.enabled ? "Pause entries" : "Resume"}</button>
      <button class="btn" data-a="reset">Reset P&L</button><button class="btn danger" data-a="delete">Delete</button></div>
    <div class="statgrid">${stat("Score", fP(r.score, 2), cls(r.score))}${stat("P&L", fSUsd(r.pnl), cls(r.pnl))}${stat("Realized", fSUsd(r.realized), cls(r.realized))}
      ${stat("Unrealized", fSUsd(r.unrealized), cls(r.unrealized))}${stat("Equity", fUsd(r.equity))}${stat("Return", fPct(r.retOnBankroll), cls(r.retOnBankroll))}
      ${stat("Trades", r.trades)}${stat("Win rate", r.winRate == null ? "–" : fNum(r.winRate * 100, 0) + "%")}${stat("Avg win", fSUsd(r.avgWin), "pos")}
      ${stat("Avg loss", fSUsd(r.avgLoss), "neg")}${stat("Best / worst", fPct(r.best, 0) + " / " + fPct(r.worst, 0))}${stat("Max drawdown", fPct(r.maxDD, 1, false))}
      ${stat("Fees paid", fUsd(r.fees))}${stat("Gas", fUsd(r.gas))}${stat("Reverted buys", r.missed)}${stat("Failed sells", r.sellFails)}</div>
    <h2>Equity</h2><div class="chart" id="eqChart"></div>
    <h2 style="margin-top:12px">Open positions (${v.positions.length})</h2>
    <div class="tablewrap"><table><tr><th>Token</th><th class="n">Held</th><th class="n">Cost</th><th class="n">Value now</th><th class="n">Net</th><th class="n">Peak</th><th class="n">Entry drift</th></tr>
      ${v.positions.map(p => `<tr><td><b>${esc(p.sym)}</b>${p.exiting ? ' <span class="tag">selling</span>' : ""} <span class="dim">${p.src}</span></td><td class="n">${fDur(p.age)}</td>
        <td class="n">${fUsd(p.cost)}</td><td class="n">${fUsd(p.value)}</td><td class="n ${cls(p.ret)}">${fPct(p.ret)}</td><td class="n">${fPct(p.peak)}</td><td class="n">${fPct(p.drift, 2)}</td></tr>`).join("") || '<tr><td colspan="7" class="empty">None</td></tr>'}</table></div>
    <h2 style="margin-top:12px">Recent trades</h2>
    <div class="tablewrap"><table><tr><th>Exit</th><th>Token</th><th class="n">Hold</th><th class="n">Cost</th><th class="n">P&L</th><th class="n">Net</th><th class="n">Peak</th><th>Reason</th></tr>
      ${v.trades.slice(0, 40).map(t => `<tr><td>${fTime(t.tOut)}</td><td><b>${esc(t.sym)}</b></td><td class="n">${fDur(t.hold)}</td><td class="n">${fUsd(t.cost)}</td>
        <td class="n ${cls(t.pnl)}">${fSUsd(t.pnl)}</td><td class="n ${cls(t.ret)}">${fPct(t.ret)}</td><td class="n">${fPct(t.peak)}</td><td class="dim">${esc(t.reason)}</td></tr>`).join("") || '<tr><td colspan="8" class="empty">No closed trades yet</td></tr>'}</table></div>`;
  equityChart($("#eqChart"), v.equity, sp.sizing.bankrollUsd);
}

$("#detail").addEventListener("click", async e => {
  const b = e.target.closest("[data-a]"); if (!b) return;
  const a = b.dataset.a;
  if (a === "close") return closeDetail();
  if (a === "edit") return openEditor(S.sel);
  if (a === "cancel") { S.editing = false; return S.sel ? loadDetail() : closeDetail(); }
  if (a === "save") return saveEditor();
  if ((a === "delete" || a === "reset") && !confirm(`${a === "delete" ? "Delete this strategy" : "Reset this strategy's P&L and positions"}?`)) return;
  const res = await api(`/api/strategy/${S.sel}/action`, { action: a });
  toast({ promote: "Promoted: evolution will no longer retire it", clone: "Cloned", pause: "Entries paused", resume: "Resumed", reset: "Reset", delete: "Deleted" }[a] || "Done");
  if (a === "delete") closeDetail(); else if (a === "clone" && res?.id) S.sel = res.id;
  await refresh(); loadDetail();
});

// ---------- editor (shared filter form builder) ----------
function filterForm(filters, prefix) {
  const m = S.meta.filters;
  let h = `<div class="form"><span></span><span class="u">min</span><span class="u">max</span>`;
  for (const [k, d] of Object.entries(m)) {
    const f = filters[k] || {};
    const lab = `<label data-tip="${esc(d.help)}">${esc(d.label)}${d.unit ? ` <span class="u">${esc(d.unit)}</span>` : ""}</label>`;
    if (d.kind === "enum") {
      h += lab + `<span style="grid-column: span 2">${d.options.map(o => `<label><input type="checkbox" data-f="${prefix}" data-k="${k}" data-o="${o}" ${f.in && f.in.includes(o) ? "checked" : ""}> ${o}</label>`).join(" &nbsp;")}</span>`;
    } else {
      h += lab + `<input type="number" step="any" data-f="${prefix}" data-k="${k}" data-m="min" value="${f.min ?? ""}"><input type="number" step="any" data-f="${prefix}" data-k="${k}" data-m="max" value="${f.max ?? ""}">`;
    }
  }
  return h + "</div>";
}
function readFilters(root, prefix) {
  const out = {};
  $$(`[data-f="${prefix}"]`, root).forEach(i => {
    const k = i.dataset.k;
    if (i.type === "checkbox") { if (i.checked) (out[k] ??= { in: [] }).in.push(i.dataset.o); }
    else if (i.value !== "") (out[k] ??= {})[i.dataset.m] = Number(i.value);
  });
  return out;
}
function fieldForm(meta, vals, prefix) {
  return Object.entries(meta).map(([k, d]) => `<label data-tip="${esc(d.help)}">${esc(d.label)} <span class="u">${esc(d.unit)}</span></label>
    <input type="number" step="any" data-g="${prefix}" data-k="${k}" value="${vals[k] ?? ""}">`).join("");
}
function readFields(root, prefix) {
  const out = {};
  $$(`[data-g="${prefix}"]`, root).forEach(i => out[i.dataset.k] = i.value === "" ? null : Number(i.value));
  return out;
}

async function openEditor(id) {
  let spec = { name: "My strategy", filters: {}, exits: S.meta.defaults.exits, sizing: S.meta.defaults.sizing };
  if (id) spec = (await api("/api/strategy/" + id)).spec;
  S.sel = id; S.editing = true;
  $("#boardSplit").classList.add("detail-open");
  const el = $("#detail"); el.classList.remove("hidden");
  el.innerHTML = `<div class="row"><h3>${id ? "Edit strategy" : "New strategy"}</h3><div class="spacer"></div><button class="btn" data-a="cancel">✕</button></div>
    <p class="help">Entry happens when a token passes the trading universe <i>and</i> every filter here. Empty = ignored. Exits use net return after all fees, impact and gas.${id ? " Editing keeps the existing P&L history; use Reset P&L for a clean comparison." : ""}</p>
    <div class="form2" style="margin-bottom:12px"><label>Name</label><input type="text" id="edName" value="${esc(spec.name)}" style="width:220px"></div>
    <h2>Entry filters</h2>${filterForm(spec.filters, "ed")}
    <h2 style="margin-top:14px">Exits</h2><div class="form2">${fieldForm(S.meta.exits, spec.exits, "ex")}</div>
    <h2 style="margin-top:14px">Sizing</h2><div class="form2">${fieldForm(S.meta.sizing, spec.sizing, "sz")}</div>
    <div class="row" style="margin-top:14px"><button class="btn primary" data-a="save">Save</button><button class="btn" data-a="cancel">Cancel</button></div>`;
  el.dataset.editId = id || "";
  if (innerWidth < 1200) el.scrollIntoView({ behavior: "smooth" });
}
async function saveEditor() {
  const el = $("#detail"), id = el.dataset.editId;
  const spec = { name: $("#edName").value || "Strategy", filters: readFilters(el, "ed"), exits: readFields(el, "ex"), sizing: readFields(el, "sz") };
  if (id) spec.id = id;
  const res = await api("/api/strategy", spec);
  S.editing = false; S.sel = res.id; toast("Saved");
  await refresh(); loadDetail();
}

// ---------- tabs + refresh loop ----------
$("#tabs").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  S.tab = b.dataset.tab;
  $$("#tabs button").forEach(x => x.classList.toggle("on", x === b));
  $$("main > section").forEach(s => s.classList.toggle("hidden", s.id !== "tab-" + S.tab));
  refreshTab(true);
});
$("#btnTheme").onclick = () => {
  const cur = document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  document.documentElement.dataset.theme = cur === "dark" ? "light" : "dark";
  try { localStorage.setItem("theme", document.documentElement.dataset.theme); } catch {}
};
try { const t = localStorage.getItem("theme"); if (t) document.documentElement.dataset.theme = t; } catch {}

async function refresh() {
  try { S.state = await api("/api/state"); } catch (e) { $("#liveDot").className = "dot bad"; $("#liveTxt").textContent = "dashboard can't reach the trader"; return; }
  renderHeader();
  if (S.tab === "board") renderBoard();
}
async function refreshTab(force) {
  if (S.tab === "board") { if (S.sel && !S.editing) loadDetail(); }
  else if (S.tab === "market") renderMarket();
  else if (S.tab === "edge") renderEdge();
  else if (S.tab === "lab") renderLab();
  else if (S.tab === "trades") renderTrades();
  else if (S.tab === "settings" && force) renderSettings();
  else if (S.tab === "backtest" && force) renderBacktest();
}
(async function boot() {
  S.meta = await api("/api/meta");
  await refresh();
  setInterval(refresh, 2000);
  setInterval(() => refreshTab(false), 3000);
})();
