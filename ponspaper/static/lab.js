// pons paper trader dashboard: genetic lab tab.
const fFit = x => x == null ? "–" : x <= -1000 ? "clone" : x <= -58 ? "too few trades" : fP(x, 2);
const PART_LABEL = { base: "Avg return − 1 std. error", simplicity: "Simplicity (per filter)", consistency: "Unprofitable training slices",
  drawdown: "Drawdown beyond 10%", activity: "Trading frequency (when profitable)", trades: "Trades (below minimum)" };
const partsTip = (p, extra = "") => p ? Object.entries(p).map(([k, v]) => `${PART_LABEL[k] || k}: <b>${k === "trades" ? v : fP(v, 2)}</b>`).join("<br>") + extra : "";
const slices = (c, folds) => c == null ? "–" : `${Math.round(c * (folds || 4))}/${folds || 4}`;

function labChart(el, hist) {
  const pts = hist.filter(h => (h.bestQual ?? (h.best > -60 ? h.best : null)) != null).map(h => ({ ...h, top: h.bestQual ?? h.best }));
  if (pts.length < 2) { el.innerHTML = `<div class="empty">The chart fills in once genomes start qualifying (enough training trades).</div>`; return; }
  const W = Math.max(320, el.clientWidth || 600), H = 200, L = 48, R = 64, T = 10, B = 22;
  const series = [["top", "Best", "var(--series-1)"], ["median", "Median", "var(--series-2)"]];
  const vals = pts.flatMap(h => series.map(s => h[s[0]]).filter(v => v != null));
  let lo = Math.min(...vals, 0), hi = Math.max(...vals, 0); const pad = (hi - lo) * 0.08 || 1; lo -= pad; hi += pad;
  const g0 = pts[0].gen, g1 = pts[pts.length - 1].gen;
  const X = g => L + ((g - g0) / (g1 - g0 || 1)) * (W - L - R), Y = v => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
  const ticks = [lo + (hi - lo) * 0.1, (lo + hi) / 2, hi - (hi - lo) * 0.1];
  const path = key => pts.filter(h => h[key] != null).map((h, i) => (i ? "L" : "M") + X(h.gen).toFixed(1) + " " + Y(h[key]).toFixed(1)).join("");
  const last = pts[pts.length - 1];
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="Lab fitness by generation">
    ${ticks.map(v => `<line x1="${L}" x2="${W - R}" y1="${Y(v)}" y2="${Y(v)}" stroke="var(--border)"/><text x="${L - 6}" y="${Y(v) + 4}" text-anchor="end" font-size="10.5" fill="var(--muted)">${fNum(v, 1)}</text>`).join("")}
    <line x1="${L}" x2="${W - R}" y1="${Y(0)}" y2="${Y(0)}" stroke="var(--muted)" stroke-dasharray="3 3"/>
    <text x="${L}" y="${H - 6}" font-size="10.5" fill="var(--muted)">gen ${g0}</text>
    <text x="${W - R}" y="${H - 6}" font-size="10.5" fill="var(--muted)" text-anchor="end">gen ${g1}</text>
    ${series.map(([k, , c]) => `<path d="${path(k)}" fill="none" stroke="${c}" stroke-width="2" stroke-linejoin="round"/>`).join("")}
    ${series.map(([k, label]) => last[k] != null ? `<text x="${W - R + 6}" y="${Y(last[k]) + 4}" font-size="11" fill="var(--text-2)">${label}</text>` : "").join("")}
    <line class="xh" y1="${T}" y2="${H - B}" stroke="var(--muted)" visibility="hidden"/>
    <rect x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}" fill="transparent"/></svg>`;
  const svg = $("svg", el), xh = $(".xh", svg);
  svg.addEventListener("mousemove", e => {
    const r = svg.getBoundingClientRect(), gx = g0 + ((e.clientX - r.left) * (W / r.width) - L) / (W - L - R) * (g1 - g0);
    let b = pts[0]; for (const h of pts) if (Math.abs(h.gen - gx) < Math.abs(b.gen - gx)) b = h;
    xh.setAttribute("x1", X(b.gen)); xh.setAttribute("x2", X(b.gen)); xh.setAttribute("visibility", "visible");
    showTip(e, `<b>Generation ${b.gen}</b><br>best ${fFit(b.top)} · median ${fFit(b.median)}<br>${b.unique} distinct strategies · mutation ${fNum(b.mutation * 100, 0)}%<br>${b.validated} champions`);
  });
  svg.addEventListener("mouseleave", () => { xh.setAttribute("visibility", "hidden"); hideTip(); });
}

function liveCell(l, deployed) {
  if (!deployed) return '<span class="dim">–</span>';
  if (!l) return '<span class="dim">trading, not judged yet</span>';
  const label = { winning: "winning", losing: "losing", failed: "failed" }[l.status] || l.status;
  return `<span class="${l.status === "winning" ? "pos" : "neg"}" data-tip="${l.n} closed trades · P&L ${fSUsd(l.pnl)}${l.why ? "<br>" + esc(l.why) : ""}">${label} ${fP(l.score, 1)}</span>`;
}

async function renderLab() {
  const v = await api("/api/lab");
  const ds = v.dataset, stat = (l, x, c = "") => `<div class="stat"><span>${l}</span><b class="${c}">${x}</b></div>`;
  const fb = v.feedback || {};
  $("#labSub").textContent = v.cfg && !v.cfg.enabled ? "off (turn it on in Filters & settings)" : v.phase;
  $("#labStats").innerHTML = stat("Phase", esc(v.phase)) + stat("Generation", fNum(v.gen, 0)) + stat("Genomes evaluated", fNum(v.evals, 0)) +
    stat("Seconds / generation", v.genSecs == null ? "–" : fNum(v.genSecs, 1)) + stat("Mutation rate", v.mutation == null ? "–" : fNum(v.mutation * 100, 0) + "%") +
    stat("Data recorded", ds ? fNum(ds.hours, 1) + "h" : "–") + stat("Entry candidates", ds ? fNum(ds.cands, 0) : "–") +
    stat("Training slices", ds ? ds.folds : "–") + stat("Validation starts", ds ? new Date(ds.split * 1000).toLocaleString() : "–") +
    stat("Data loaded", ds ? fTime(ds.builtAt) : "–") +
    stat("Live results fed back", `<span class="pos">${fb.winning || 0} win</span> · <span class="neg">${(fb.losing || 0) + (fb.failed || 0)} lose</span>`);
  const dropped = (v.dropped || []).length ? `<br>Dropped on the last data refresh: ${v.dropped.map(d => esc(d.why)).join(", ")}` : "";
  $("#labErr").innerHTML = esc(v.lastError || "") + `<span class="dim">${dropped}</span>`;
  $("#labLegend").innerHTML = `<span><i style="background:var(--series-1)"></i> Best fitness</span><span><i style="background:var(--series-2)"></i> Median fitness</span>`;
  labChart($("#labChart"), v.history || []);
  const br = r => r ? `${r.n} · ${fPct(r.mean, 1)}` : "–";
  const folds = ds ? ds.folds : 4;
  $("#labHall").innerHTML = `<tr><th>Strategy</th><th class="n">Fitness</th><th class="n">Training</th><th class="n">Slices</th><th class="n">Validation</th><th class="n">Stress test</th><th>Live</th><th></th></tr>` +
    ((v.hall || []).map(h => `<tr><td><div class="desc">${esc(h.desc)}</div></td>
      <td class="n ${cls(h.fitness)}" data-tip="${esc(partsTip(h.parts))}">${fFit(h.fitness)}</td>
      <td class="n" data-tip="trades · average net return">${br(h.train)}</td>
      <td class="n" data-tip="${esc((h.folds || []).map((f, i) => `slice ${i + 1}: ${f[0]} trades, ${f[1] == null ? "–" : fPct(f[1], 1)}`).join("<br>"))}">${slices(h.consistency, folds)}</td>
      <td class="n ${cls(h.val && h.val.mean)}">${br(h.val)}</td>
      <td class="n ${cls(h.stress && h.stress.mean)}" data-tip="whole window with extra latency and fees">${br(h.stress)}</td>
      <td>${liveCell(h.live, h.deployed)}</td>
      <td>${h.deployed ? '<span class="tag manual">live</span>' : `<button class="btn" data-deploy="${esc(h.key)}">Deploy live</button>`}</td></tr>`).join("")
      || `<tr><td colspan="8" class="empty">No champions yet. A genome must be profitable in most training slices, on the validation data (15+ trades) and under the stress test. With the market punishing nearly everything, an empty list is an honest answer.</td></tr>`);
  $("#labTop").innerHTML = `<tr><th>Strategy</th><th class="n">Fitness</th><th class="n">Selection</th><th class="n">Filters</th><th class="n">Slices</th><th class="n">Trades</th><th class="n">Avg/trade</th><th class="n">P&L</th></tr>` +
    ((v.top || []).map(t => `<tr style="opacity:${t.clone ? 0.5 : 1}"><td><div class="desc">${esc(t.desc)}</div></td>
      <td class="n ${cls(t.fitness)}" data-tip="${esc(partsTip(t.parts))}">${fFit(t.fitness)}</td>
      <td class="n" data-tip="Fitness used for breeding: minus ${t.similar} similar genomes${t.feedback ? `, ${fP(t.feedback, 1)} from live results` : ""}">${t.clone ? "clone" : fFit(t.sel)}</td>
      <td class="n">${t.nfilters ?? "–"}</td><td class="n">${slices(t.consistency, folds)}</td>
      <td class="n">${t.train.n}</td><td class="n ${cls(t.train.mean)}">${fPct(t.train.mean)}</td>
      <td class="n ${cls(t.train.pnl)}">${fSUsd(t.train.pnl)}</td></tr>`).join("") || `<tr><td colspan="8" class="empty">Waiting for the first generation.</td></tr>`);
}
$("#labHall").addEventListener("click", async e => {
  const b = e.target.closest("[data-deploy]"); if (!b) return;
  const r = await api("/api/lab/deploy", { key: b.dataset.deploy });
  toast(r.added && r.added.length ? "Deployed: now paper trading live" : "Already live"); renderLab(); refresh();
});
$("#btnLabRebuild").onclick = async () => { await api("/api/lab/rebuild", {}); toast("Reloading recorded data"); };
