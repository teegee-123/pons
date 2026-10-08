// pons paper trader dashboard: market, edge map, trades, settings, backtest views.

// ---------- market ----------
$("#mktUni").onchange = () => renderMarket();
async function renderMarket() {
  const v = await api("/api/market");
  let toks = v.tokens;
  if ($("#mktUni").checked) toks = toks.filter(t => t.inUniverse);
  $("#mktSub").textContent = `${toks.length} shown, most recently traded first`;
  const chg = x => `<td class="n ${cls(x)}">${fP(x, 1)}</td>`;
  $("#mktTbl").innerHTML = `<tr><th>Token</th><th>Stage</th><th class="n">Age</th><th class="n">Mcap</th><th class="n">Progress</th><th class="n">1m</th><th class="n">5m</th><th class="n">15m</th>
    <th class="n">Trades/min</th><th class="n">Vol/min</th><th class="n" title="tick data">Buy share</th><th class="n" title="tick data">Net flow</th><th class="n" title="tick data">Buyers 5m</th><th class="n" title="tick data">Biggest buy</th><th class="n" title="tick data">Spike</th><th class="n" title="tick data">Creator sold</th><th class="n">Below peak</th><th class="n">Trades</th><th class="n">Tax</th><th class="n">Socials</th><th class="n">Idle</th><th>Universe</th><th class="n">Held by</th></tr>` +
    toks.map(t => `<tr><td><a href="https://robin.etherscan.io/token/${t.addr}" target="_blank" rel="noopener"><b>${esc(t.sym)}</b></a> <span class="dim">${esc((t.name || "").slice(0, 22))}</span>${t.quote !== "ETH" ? ` <span class="tag">${esc(t.quote)}</span>` : ""}</td>
      <td>${esc(t.stage)}</td><td class="n">${fMin(t.ageMin)}</td><td class="n">${fK(t.mcapUsd)}</td><td class="n">${t.progressPct == null ? "–" : fNum(t.progressPct, 0) + "%"}</td>
      ${chg(t.chg1m)}${chg(t.chg5m)}${chg(t.chg15m)}<td class="n">${t.tpm1 == null ? "–" : fNum(t.tpm1, 1)}</td><td class="n">${t.vol1m == null ? "–" : fUsd(t.vol1m, 0)}</td>
      <td class="n">${t.buyRatio1m == null ? "–" : fNum(t.buyRatio1m, 0) + "%"}</td><td class="n ${cls(t.netFlow1m)}">${t.netFlow1m == null ? "–" : fSUsd(t.netFlow1m, 0)}</td>
      <td class="n">${t.buyers5m ?? "–"}</td><td class="n">${t.whale1m == null ? "–" : fUsd(t.whale1m, 0)}</td>
      <td class="n">${t.volSpike == null ? "–" : fNum(t.volSpike, 1) + "×"}</td><td class="n ${t.devSoldUsd > 0 ? "neg" : ""}">${t.devSoldUsd == null ? "–" : fUsd(t.devSoldUsd, 0)}</td>
      <td class="n">${t.ddPeak == null ? "–" : fNum(t.ddPeak, 0) + "%"}</td><td class="n">${fNum(t.tradeCount, 0)}</td><td class="n">${t.taxBps}</td><td class="n">${t.socials}</td>
      <td class="n">${fDur(t.idleSec)}</td><td>${t.inUniverse ? "✓ yes" : '<span class="dim">no</span>'}</td><td class="n">${t.held || ""}</td></tr>`).join("");
}

// ---------- edge map ----------
const FEAT_LABEL = k => (S.meta.filters[k] ? S.meta.filters[k].label + (S.meta.filters[k].unit ? ` (${S.meta.filters[k].unit})` : "") : k);
function divColor(mean, cap = 0.15) {
  if (mean == null) return "transparent";
  const a = Math.min(1, Math.abs(mean) / cap);
  return `color-mix(in srgb, ${mean >= 0 ? "var(--div-pos)" : "var(--div-neg)"} ${Math.round(12 + a * 78)}%, var(--div-mid))`;
}
async function renderEdge() {
  const v = await api("/api/edge?h=" + S.h);
  $("#hChips").innerHTML = v.horizons.map((m, i) => `<button data-h="${i}" class="${i === v.h ? "on" : ""}">${m}m</button>`).join("");
  $("#edgeSub").textContent = `${v.samples} complete samples, ${v.pending} in flight`;
  const b = v.base, stat = (l, x, c = "") => `<div class="stat"><span>${l}</span><b class="${c}">${x}</b></div>`;
  $("#edgeBase").innerHTML = stat("Baseline avg (all samples)", b.n ? fPct(b.mean, 2) : "–", cls(b.mean)) + stat("Baseline win rate", b.n ? fNum(b.win * 100, 0) + "%" : "–") +
    stat("Samples at this horizon", fNum(b.n || 0, 0)) + stat("Std. error", b.se == null ? "–" : "±" + fNum(b.se * 100, 2) + "%");
  // heatmap
  const ax = v.gridAxes, ages = ax.ageMin, mcs = ax.mcapUsd;
  $("#heatLegend").innerHTML = `<span>loses after costs</span><i style="background:${divColor(-0.15)}"></i><i style="background:${divColor(-0.05)}"></i><i style="background:var(--div-mid)"></i><i style="background:${divColor(0.05)}"></i><i style="background:${divColor(0.15)}"></i><span>profits after costs (≥15% saturates)</span>`;
  $("#heatTbl").innerHTML = `<tr><th>Age ↓ / Mcap →</th>${mcs.map(m => `<th>${esc(m)}</th>`).join("")}</tr>` + ages.map((a, i) => `<tr><th>${esc(a)}</th>${mcs.map((m, j) => {
    const c = v.grid[`${i},${j}`];
    if (!c || !c.n) return `<td style="background:transparent" class="dim">·</td>`;
    const strong = Math.abs(c.mean) > 0.09;
    return `<td style="background:${divColor(c.mean)};color:${strong ? "#fff" : "var(--text)"};opacity:${c.n < 20 ? 0.55 : 1}" data-tip="Age ${esc(a)}, mcap ${esc(m)}<br>avg <b>${fPct(c.mean, 2)}</b>, win ${fNum(c.win * 100, 0)}%<br>${c.n} samples">${fPct(c.mean, 1)}<br><span style="font-size:10px;opacity:.8">n=${c.n}</span></td>`;
  }).join("")}</tr>`).join("");
  // per-feature diverging bars
  $("#edgeFeats").innerHTML = Object.entries(v.features).map(([k, rows]) => {
    const cap = Math.max(0.05, ...rows.filter(r => r.n >= 5).map(r => Math.abs(r.mean)));
    return `<div class="card"><h2>${esc(FEAT_LABEL(k))}</h2><table><tr><th>Bucket</th><th class="n">n</th><th class="n">Avg</th><th class="n">Win</th><th style="width:40%"></th></tr>` +
      rows.map(r => {
        if (!r.n) return `<tr class="dim"><td>${esc(r.label)}</td><td class="n">0</td><td></td><td></td><td></td></tr>`;
        const w = Math.min(50, Math.abs(r.mean) / cap * 50);
        const bar = `<div class="bar"><div class="mid"></div><div class="fill" style="${r.mean >= 0 ? `left:50%` : `left:${50 - w}%`};width:${w}%;background:${r.mean >= 0 ? "var(--div-pos)" : "var(--div-neg)"}"></div></div>`;
        const delta = b.n ? r.mean - b.mean : null;
        return `<tr style="opacity:${r.n < 20 ? 0.5 : 1}" data-tip="${esc(FEAT_LABEL(k))} ${esc(r.label)}<br>avg ${fPct(r.mean, 2)} (${delta == null ? "" : fPct(delta, 2) + " vs baseline"})<br>win ${fNum(r.win * 100, 0)}%, ${r.n} samples${r.se != null ? `, ±${fNum(r.se * 100, 2)}%` : ""}">
          <td>${esc(r.label)}</td><td class="n">${r.n}</td><td class="n ${cls(r.mean)}">${fPct(r.mean, 1)}</td><td class="n">${fNum(r.win * 100, 0)}%</td><td>${bar}</td></tr>`;
      }).join("") + "</table></div>";
  }).join("");
}
$("#hChips").addEventListener("click", e => { const b = e.target.closest("button"); if (!b) return; S.h = Number(b.dataset.h); renderEdge(); });

// ---------- trades ----------
async function renderTrades() {
  const v = await api("/api/trades");
  $("#closedTbl").innerHTML = `<tr><th>Exit</th><th>Strategy</th><th>Token</th><th class="n">Hold</th><th class="n">Cost</th><th class="n">Proceeds</th><th class="n">P&L</th><th class="n">Net</th><th class="n">Peak</th><th class="n">Fees</th><th class="n">Drift in</th><th>Reason</th><th>Fill</th></tr>` +
    (v.closed.map(t => `<tr><td>${fTime(t.tOut)}</td><td>${esc(t.name)}</td><td><b>${esc(t.sym)}</b></td><td class="n">${fDur(t.hold)}</td><td class="n">${fUsd(t.cost)}</td><td class="n">${fUsd(t.proceeds)}</td>
      <td class="n ${cls(t.pnl)}">${fSUsd(t.pnl)}</td><td class="n ${cls(t.ret)}">${fPct(t.ret)}</td><td class="n">${fPct(t.peak)}</td><td class="n">${fUsd(t.fees)}</td><td class="n">${fPct(t.driftIn, 2)}</td>
      <td class="dim">${esc(t.reason)}</td><td class="dim">${esc(t.src)}</td></tr>`).join("") || `<tr><td colspan="13" class="empty">No closed trades yet.</td></tr>`);
  $("#fillsTbl").innerHTML = `<tr><th>Time</th><th>Strategy</th><th>Side</th><th>Token</th><th>Result</th><th class="n">Latency</th><th class="n">USD</th><th class="n">Drift</th><th class="n">P&L</th><th>Reason</th></tr>` +
    (v.fills.map(f => `<tr><td>${fTime(f.t)}</td><td>${esc(f.name)}</td><td>${f.side}</td><td><b>${esc(f.sym)}</b></td>
      <td>${f.ok ? `filled <span class="dim">(${esc(f.src)})</span>` : `<span class="neg">reverted</span> <span class="dim">${esc(f.err || "")}</span>`}</td>
      <td class="n">${f.latMs}ms</td><td class="n">${fUsd(f.usd)}</td><td class="n" data-tip="Spot at fill vs spot the strategy saw when it decided">${fPct(f.drift, 2)}</td>
      <td class="n ${cls(f.pnl)}">${f.pnl == null ? "" : fSUsd(f.pnl)}</td><td class="dim">${esc(f.reason)}</td></tr>`).join("") || `<tr><td colspan="10" class="empty">No fills yet.</td></tr>`);
}

// ---------- settings ----------
const EX_META = {
  latencyMs: ["Latency", "ms", "Delay from the strategy seeing data to the transaction executing. Fills use on-chain state at that moment."],
  gasUsd: ["Gas per tx", "$", "Charged on every transaction, including reverted ones"],
  buySlippagePct: ["Buy slippage limit", "%", "Buy reverts if it would get this much fewer tokens than quoted at signal time"],
  sellSlippagePct: ["Sell slippage limit", "%", "Sell reverts beyond this; after 3 failed sells the position is dumped at any price"],
  protocolFeeBps: ["Curve protocol fee", "bps", "On-chain value is 100 (1%), charged on buys and sells, plus the token's creator tax"],
  hookFeeBps: ["Graduated pool fee", "bps", "v4 hook fee on graduated tokens (on-chain: 100), plus creator tax"],
  gradLiquidityMult: ["Graduated depth ×", "", "Scales the modelled v4 pool depth used for marking graduated positions"],
};
const EVO_META = {
  population: ["Auto population", "#", "Number of auto strategies running at once"],
  epochMin: ["Epoch length", "min", "How often the worst auto strategies are retired and replaced"],
  minTrades: ["Min trades to judge", "#", "A strategy is judged once it has this many closed trades..."],
  judgeAfterMin: ["...or after", "min", "...or once it has been alive this long with any trade or open position"],
  stuckMin: ["Retire if stuck for", "min", "Retire an auto strategy that is losing overall and has held a position under water this long"],
  cullFrac: ["Cull fraction", "0-1", "Share of judged strategies retired each epoch (only losers unless the field is crowded)"],
  mutateFrac: ["Mutate fraction", "0-1", "Share of replacements bred from winners (the rest are random, for exploration)"],
  idleEpochs: ["Retire idle after", "epochs", "Auto strategies that never trade are replaced after this many epochs"],
  shrinkK: ["Score shrinkage", "trades", "Phantom zero-return trades added to the score; higher = more skeptical of small samples"],
};
const LAB_META = {
  windowHours: ["Data window", "h", "How many hours of recorded data the lab trains and validates on"],
  validateFrac: ["Validation share", "0-1", "Most recent share of the window held back for validation"],
  population: ["Population", "#", "Genomes per generation"],
  elite: ["Elites", "#", "Best genomes copied unchanged into the next generation"],
  tournament: ["Tournament size", "#", "Parents are the best of this many random picks; larger = greedier"],
  crossover: ["Crossover rate", "0-1", "Share of children made by mixing two parents (the rest are mutated copies)"],
  mutation: ["Base mutation rate", "0-1", "Chance each gene changes; rises automatically when progress stalls"],
  immigrants: ["Immigrants", "0-1", "Share of each generation that is brand-new random genomes"],
  minTrades: ["Min trades (train)", "#", "Genomes with fewer training trades rank below every qualifying one"],
  minValTrades: ["Min trades (validation)", "#", "Validation trades needed before a genome can be a champion"],
  promoteCount: ["Champions per epoch", "#", "Validated genomes injected into live trading at each live epoch"],
  rebuildMin: ["Refresh data every", "min", "How often the recorded window is reloaded"],
  maxGensPerData: ["Generations per refresh", "#", "Pause after this many generations on the same data (more would only overfit it)"],
  folds: ["Training slices", "#", "The training data is cut into this many consecutive slices; genomes are rewarded for profiting in most of them"],
  minFoldShare: ["Champion: slices profitable", "0-1", "Share of training slices a champion must be profitable in"],
  stressLatencyMs: ["Stress: extra latency", "ms", "Champions must stay profitable with this much extra delay..."],
  stressFeeBps: ["Stress: extra fees", "bps", "...and this much extra cost per trade side"],
  complexityPenalty: ["Simplicity penalty", "per filter", "Fitness points removed per active filter; simpler rules generalise better"],
  consistencyPenalty: ["Consistency penalty", "points", "Removed in proportion to the training slices that weren't profitable"],
  ddPenalty: ["Drawdown penalty", "per %", "Points removed per % of drawdown beyond 10% of bankroll"],
  activityBonus: ["Activity bonus", "points", "Bonus for trading more often, only when profitable"],
  nicheSimilarity: ["Similarity threshold", "0-1", "Two genomes sharing this share of their trades count as the same idea"],
  nichePenalty: ["Diversity penalty", "per twin", "Breeding fitness removed per similar genome, to keep different ideas alive"],
  feedbackPenalty: ["Live failure penalty", "points", "Breeding fitness removed from close relatives of champions that failed live"],
  feedbackBonus: ["Live winner bonus", "points", "Breeding fitness added to close relatives of champions winning live"],
  duty: ["CPU share", "0-1", "Fraction of one CPU the lab may use (keep low on small servers)"],
};
const POLL_META = {
  "poll.intervalSec": ["Poll interval", "s", "How often /api/launches?sort=active is fetched"],
  "poll.pages": ["Pages per poll", "#", "40 tokens per page"],
  "edge.sampleEverySec": ["Edge sample every", "s", "Per-token sampling interval for the edge map"],
  "edge.sizeUsd": ["Edge sample size", "$", "Hypothetical trade size used by the edge map"],
};
const fld = (id, [l, u, h], v) => `<label data-tip="${esc(h)}">${esc(l)} <span class="u">${esc(u)}</span></label><input type="number" step="any" data-s="${id}" value="${v ?? ""}">`;
const chk = (id, l, h, v) => `<label data-tip="${esc(h)}">${esc(l)}</label><input type="checkbox" data-s="${id}" ${v ? "checked" : ""}>`;
function renderSettings() {
  const c = S.state.cfg;
  $("#uniForm").innerHTML = filterForm(c.universe, "uni");
  $("#exForm").innerHTML = Object.entries(EX_META).map(([k, m]) => fld("execution." + k, m, c.execution[k])).join("") +
    chk("execution.useChainQuotes", "Fill on live on-chain quotes", "Off = fill from API data with the curve model (latency then has little effect)", c.execution.useChainQuotes);
  $("#sizeForm").innerHTML = Object.entries(S.meta.sizing).map(([k, d]) => fld("sizing." + k, [d.label, d.unit, d.help], c.sizing[k])).join("");
  $("#evoForm").innerHTML = chk("evolution.enabled", "Evolution on", "Retire losers and breed winners every epoch", c.evolution.enabled) +
    Object.entries(EVO_META).map(([k, m]) => fld("evolution." + k, m, c.evolution[k])).join("") +
    chk("evolution.retireClones", "Retire clones", "Retire auto strategies that make exactly the same trades as an older one", c.evolution.retireClones);
  $("#labForm").innerHTML = chk("lab.enabled", "Lab on", "Run the genetic algorithm in the background on recorded data", c.lab.enabled) +
    Object.entries(LAB_META).map(([k, m]) => fld("lab." + k, m, c.lab[k])).join("");
  $("#pollForm").innerHTML = Object.entries(POLL_META).map(([k, m]) => { const [a, b] = k.split("."); return fld(k, m, c[a][b]); }).join("") +
    chk("edge.enabled", "Edge map sampling", "Collect forward-return samples", c.edge.enabled) +
    chk("record.enabled", "Record snapshots", "Write every poll to data/snapshots for backtests", c.record.enabled);
  $("#setMsg").textContent = "";
}
$("#btnSaveSettings").onclick = async () => {
  const patch = { universe: readFilters($("#uniForm"), "uni") };
  $$("[data-s]").forEach(i => {
    const [a, b] = i.dataset.s.split(".");
    const v = i.type === "checkbox" ? i.checked : (i.value === "" ? null : Number(i.value));
    if (v === null) return;
    (patch[a] ??= {})[b] = (a === "sizing" && b === "maxOpen") || ["population", "minTrades", "idleEpochs", "pages", "elite", "tournament", "minValTrades", "promoteCount", "maxGensPerData", "folds", "stressLatencyMs", "stressFeeBps"].includes(b) ? Math.round(v) : v;
  });
  await api("/api/settings", patch);
  await refresh(); renderSettings();
  $("#setMsg").textContent = "Saved. Applies from the next poll.";
  toast("Settings saved");
};
$("#btnResetAll").onclick = async () => {
  if (!confirm("Delete every strategy's positions, trades and stats, and start a fresh population?")) return;
  await api("/api/reset", {}); closeDetail(); toast("Reset"); refresh();
};

// ---------- backtest ----------
async function renderBacktest() {
  const v = await api("/api/replay");
  if (!v.rows || !v.rows.length) { $("#btMeta").textContent = "No backtest results yet."; $("#btTbl").innerHTML = ""; return; }
  $("#btMeta").textContent = `Run at ${new Date(v.at * 1000).toLocaleString()}: ${fNum(v.hours, 1)}h of data, ${v.snapshots} snapshots, ${v.files.length} file(s).`;
  $("#btTbl").innerHTML = `<tr><th>Strategy</th><th class="n">Score</th><th class="n">Trades</th><th class="n">Win</th><th class="n">P&L</th><th class="n">Max DD</th><th></th></tr>` +
    v.rows.map((r, i) => `<tr><td><span class="tag ${r.kind}">${r.kind}</span><b>${esc(r.name)}</b><div class="desc">${esc(r.desc)}</div></td><td class="n ${cls(r.score)}">${fP(r.score, 2)}</td>
      <td class="n">${r.trades}</td><td class="n">${r.winRate == null ? "–" : fNum(r.winRate * 100, 0) + "%"}</td><td class="n ${cls(r.pnl)}">${fSUsd(r.pnl)}</td><td class="n">${fPct(r.maxDD, 1, false)}</td>
      <td><button class="btn" data-adopt="${i}">Adopt</button></td></tr>`).join("");
  $("#btTbl").onclick = async e => {
    const b = e.target.closest("[data-adopt]"); if (!b) return;
    const r = v.rows[Number(b.dataset.adopt)];
    await api("/api/strategy", { name: "BT " + r.name, filters: r.spec.filters, exits: r.spec.exits, sizing: r.spec.sizing });
    toast("Adopted: now paper trading live"); refresh();
  };
}
