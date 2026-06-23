"use strict";
// agent-insight A 形态 · fleet renderer (视觉增强版 · §8.3/§8.9).
// 消费 /api/result (analyze.py --json 产物). hero 左=cache命中率(input-side 头条)+per-session 分布;
// 右=context 压力 (Plan 3a: 真 per-session ctx 峰值分布). fleet 表: monospace + cache% 色条 + outlier spotlight + ctx peak 真数字 (⚠ glyph 当 > CTX_LARGE).
// 边界(诚实): ctx peak 仅 root 主线 usage 抽样; 无 usage session = 淡灰 — (不伪造).
// 色阶(绝对阈值): cr% ≥85 绿 / 60–85 琥珀 / <60 红; 0-token session = empty(淡灰, 不告警, 不当 outlier).

const CTX_LARGE = 160000;   // ctx "大上下文" 提示阈值 (80% × 200K 保守基线). 注: 是绝对大小提示, 非逼近模型上限 (reader 不知各 session 的 window). glyph: peak>CTX_LARGE → ⚠
const fmt = (n) => (n == null ? "—" : Number(n).toLocaleString("en-US"));
const fmtK = (n) => (n == null ? "—" : n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? (n / 1e3).toFixed(1) + "k" : String(n));
const pct = (num, den) => (den ? (num / den * 100).toFixed(1) + "%" : "—");
// 严格 input 侧命中率 = cacheRead / (cacheRead + input + cacheCreation) (§8.3 头条口径; output 永不进缓存)
const hitInputSide = (g) => {
  const den = (g.cacheRead || 0) + (g.input || 0) + (g.cacheCreation || 0);
  return den ? (g.cacheRead || 0) / den : null;
};
// 合并 subagent (grandTotal) + root 主线 (rootUsage) 的计费三桶 (2026-06-19 统一计费口径).
// 两源 cacheRead/input/cc 都是独立真实计费事件, 累加非重复 (用户原则: 不被重复算钱就算上).
// 纯 root session (无 subagent) 现也走此口径 → 有真实命中率, 不再显 — (取代旧 totalTokens<=0 兜底).
const billable = (r) => {
  const g = r.grandTotal || {}, ru = r.rootUsage || {};
  return {
    cacheRead: (g.cacheRead || 0) + (ru.cacheRead || 0),
    input: (g.input || 0) + (ru.input || 0),
    cacheCreation: (g.cacheCreation || 0) + (ru.cacheCreation || 0),
  };
};
// 计费口径 cache 命中率 = 合并 cacheRead / (合并 cacheRead + input + cacheCreation). output 永不进缓存.
const hitBillable = (r) => {
  const b = billable(r);
  const den = b.cacheRead + b.input + b.cacheCreation;
  return den ? b.cacheRead / den : null;
};
const billableTotal = (r) => { const b = billable(r); return b.cacheRead + b.input + b.cacheCreation; };
// per-session 命中: 统一计费口径 (root+sub 合并); 算不出 (空壳/纯 skill session) → null → 显 —.
const sessHit = (r) => hitBillable(r);
// 绝对阈值色阶 (empty = 0-token 空壳, 中性灰, 不告警)
const tierOf = (frac, total) =>
  (total == null || total <= 0) ? "empty"
  : (frac == null) ? "na"
  : (frac >= 0.85) ? "ok"
  : (frac >= 0.60) ? "mid"
  : "lo";
// fleet 异常信号 §8.3 — banner 三类独立计数 + 健康列取最严重 lvl (cell/排序键共用, DRY). SkillCall 零 token 不进此口径.
const isBlown = (r) => !!(r.ctxLimitErrors && r.ctxLimitErrors.count > 0);          // 💥 压缩失败/逼爆
const isLowHit = (r) => {                                                            // ⚠ 低命中 <60% (banner 独立计数, 不看 ctx; 可与爆掉重叠)
  const bt = billableTotal(r), h = sessHit(r);
  return bt > 0 && h != null && tierOf(h, bt) === "lo";
};
// 健康列取最严重 lvl: 爆(3) > 低命中(2) > 异步(1) > 正常(0). banner 三项独立计数, 此处取 max 供 cell 显一图标 + 排序键.
const anomalyLvl = (r) =>
  isBlown(r) ? 3 : isLowHit(r) ? 2 : ((r.asyncCount || 0) > 0 ? 1 : 0);
const barClass = (frac) =>
  frac == null ? "" : (frac >= 0.85 ? "hit-h" : frac >= 0.60 ? "hit-m" : "hit-l");
// 缺口 1 预算 chip (reader-computes; result.generations[i].budgetState 配了 threshold 才存在, 未配 → "" 不显, 表格逐字同今天).
// budgetState = {threshold, cumulativeTotal, pctOfThreshold, exceeded}. tier 与 cache 色阶相反 (预算: 越近/超阈越红; cache: 越高越绿), 故独立 tier:
// <80% ok(绿) / 80–99% warn(琥珀) / ≥100% 或 exceeded over(红). per-session 行查其 generation 的 budgetState (singleton/multiSession 成员同源).
function budgetChip(bs) {
  if (!bs) return "";
  const p = Number(bs.pctOfThreshold) || 0;
  const tier = (bs.exceeded || p >= 100) ? "over" : p >= 80 ? "warn" : "ok";
  const tip = `预算 ${fmt(bs.cumulativeTotal)} / ${fmt(bs.threshold)} token (${p.toFixed(1)}%${bs.exceeded ? " · 已超阈" : ""})`;
  return `<span class="budget-meter budget-${tier}" title="${esc(tip)}">·${Math.round(p)}%</span>`;
}

// ctx peak 单元格: 三态 — 💥 爆掉(ctxLimitErrors.count>0, 压缩失败/逼爆, §8.3) > ⚠ 大上下文(peak>CTX_LARGE) > 正常.
// 💥 优先级最高: 9aa81da2 peak 201k 且爆掉 → 显 💥 (非 ⚠), 呼应 §8.3:496「peak 才 ~20% 却爆, 非逼近 limit」.
function ctxCell(peak, ctxErr) {
  const blown = ctxErr && ctxErr.count > 0;
  if (blown) {
    const sample = String(ctxErr.sample || "API Error: ... context window limit").slice(0, 100);
    return `<span class="ctx-blown" title="💥 压缩失败爆掉 (${fmt(peak)}; ${ctxErr.count}× ${sample})">💥 ${fmt(peak)}</span>`;
  }
  if (peak == null || peak <= 0) return `<span class="faint">—</span>`;
  const warn = peak > CTX_LARGE;
  return warn
    ? `<span class="ctx-warn" title="大绝对上下文 (${fmt(peak)} > ${CTX_LARGE}; 仅提示绝对大小, 非逼近模型上限)">⚠ ${fmt(peak)}</span>`
    : `<span>${fmt(peak)}</span>`;
}
// 健康列单格 (D-A2): 取最严重 lvl 显一图标 (💥/⚠/⏳/✓), 与 banner 三类呼应; 排序键 desc → 问题 session 沉顶.
function healthCell(r) {
  const lvl = anomalyLvl(r);
  if (lvl === 3) return `<span class="lo badge" title="💥 ctx 爆掉 (压缩失败/逼爆)">💥</span>`;
  if (lvl === 2) return `<span class="lo badge" title="低命中 <60%">⚠</span>`;
  if (lvl === 1) return `<span class="mid badge" title="异步未回报 ${r.asyncCount || 0}">⏳</span>`;
  return `<span class="ok badge" title="正常">✓</span>`;
}

function fmtDur(s) {
  if (s == null) return "—";
  if (s >= 3600) return (s / 3600).toFixed(1) + "h";
  if (s >= 60) return (s / 60).toFixed(1) + "m";
  return Math.round(s) + "s";
}
function fmtDurMs(ms) {                         // ms → "1h 2m 3s" / "2m 3s" / "3s" / "450ms" (用户: ms 不直观)
  if (ms == null || isNaN(ms) || ms < 0) return "—";
  if (ms < 1000) return Math.round(ms) + "ms";
  let t = Math.round(ms / 1000);                // → 整秒
  const h = Math.floor(t / 3600); t -= h * 3600;
  const m = Math.floor(t / 60); t -= m * 60;
  const parts = [];
  if (h) parts.push(h + "h");
  if (m) parts.push(m + "m");
  parts.push(t + "s");
  return parts.join(" ");
}
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
function projName(p) { return esc(String(p || "?").replace(/^-home-/, "")); }
// 原始去前缀 project 名 (未 esc): 供 row title 拼接 (长 session 名的全名补充到 hover 提示前, 补充非替换); 调用方整体 esc. 显示用 projName.
function projNameRaw(p) { return String(p || "?").replace(/^-home-/, ""); }
// dist-row session 区分前缀 (raw, 调用方整体 esc): 完整 project 名 + 短 sid(唯一区分同 project 多 session) + 时长 + spawns.
// 单取 project 名无法区分其下多 session (显示同名); 加 sid/duration/spawns 才能在 hover 时辨清是哪一个 session.
function sessPrefix(r) {
  const sid = String(r.sid || "").slice(0, 8);
  return projNameRaw(r.project) + " · sid " + sid + " · " + fmtDur(r.durationS) + " · " + (r.spawns || 0) + "sp";
}
// mode chip 友好名: 后端术语 (scan-projects / own-JSONL / live / transcript) → 中文; 未知 → 原样兜底
// --- fleet 列排序 (§8.3 fleet 总览表): 默认 total desc; 点列头切列, 同列再点切换升降序; live-tail 2s 重渲染保留选中列 (不跳回 total) ---
let _lastResult = null;
let _sort = { col: "total", dir: "desc" };
let _skillExpandedName = null;   // 当前展开的 skill 行名 (跨 live-tail 2s 重渲染保留; toggle 维护; 切源清空)
let _liveTailOn = true;          // 顶部 mode chip = 刷新轴 (live-tail 开关): ●实时 / ⏸暂停; 与数据源 (result.isLive 来源轴) 无关, 来源在下方"数据源"下拉
let _lastDataActive = true;      // 数据活性 (render 据 result.dataAgeSeconds 刷新): true=源在动 → ●实时 / false=源长期静止 (旧 session 不再活动) → ⏳静止; null 信号默认当在动 (不冤枉)
const STALE_AFTER_S = 300;       // "静止"阈值: 文件最新更新距今 > 5 分钟 → 视为旧 session 不再活动 (CC 思考/跑工具可数分钟不写盘, 故宽容); 文件恢复更新下次轮询即转实时
const FLEET_COLS = [
  { key: "session", txt: true, get: r => projName(r.project) },
  { key: "spawns",  get: r => r.spawns || 0 },
  { key: "dur",     get: r => r.durationS || 0 },
  { key: "total",   get: r => billableTotal(r) },                                          // 合并 root+sub 计费总量 (与 cache 面板同口径)
  { key: "cache",   get: r => { const h = sessHit(r); return h != null ? h * 100 : -1; } }, // 合并命中率 (root主线+subagent)
  { key: "fullin",  get: r => { const b = billable(r); return b.input + b.cacheCreation; } }, // 合并全价 input
  { key: "ctx",     get: r => r.ctxPeak || 0 },
  { key: "ok",      get: r => anomalyLvl(r) },                                   // 健康 lvl (爆3>低命中2>异步1>正常0); defaultDir desc → 问题 session 沉顶
];
function defaultDir(key) { return key === "session" ? "asc" : "desc"; }
function sortRows(rows) {
  const col = FLEET_COLS.find(c => c.key === _sort.col) || FLEET_COLS[3];  // 兜底 total
  const dir = _sort.dir === "asc" ? 1 : -1;
  return rows.slice().sort((a, b) => {
    const av = col.get(a), bv = col.get(b);
    return col.txt ? dir * String(av).localeCompare(String(bv)) : dir * ((av || 0) - (bv || 0));
  });
}
function updateSortHeader() {
  document.querySelectorAll("#fleet-table thead th").forEach(th => {
    let sp = th.querySelector(".sort");
    if (!sp) { sp = document.createElement("span"); sp.className = "sort"; th.appendChild(sp); }
    const active = th.dataset.col === _sort.col;
    sp.textContent = active ? (_sort.dir === "asc" ? "▲" : "▼") : "";
    th.classList.toggle("sort-active", active);
  });
}
function initSort() {
  const thead = document.querySelector("#fleet-table thead");
  if (!thead) return;
  thead.addEventListener("click", e => {
    const th = e.target.closest("th");
    if (!th || !th.dataset.col) return;
    const key = th.dataset.col;
    if (_sort.col === key) _sort.dir = _sort.dir === "asc" ? "desc" : "asc";
    else { _sort.col = key; _sort.dir = defaultDir(key); }
    updateSortHeader();
    if (_lastResult) render(_lastResult);   // 重渲染 tbody 应用新序; _sort 模块级, live-tail 下轮也保留
  });
  updateSortHeader();
}

function render(result) {
  _lastResult = result;
  const gt = result.grandTotal || {};
  // 头条数字: subagent grandTotal + 跨 session 累加 root 主线 rootUsage (2026-06-19 统一计费口径;
  // root 主线 cacheRead 常是 subagent 的几十倍, 旧口径只算 sub 严重低估 fleet 真实命中率).
  const fleetRoot = (result.perSession || []).reduce((a, r) => {
    const ru = r.rootUsage || {};
    a.cacheRead += ru.cacheRead || 0; a.input += ru.input || 0; a.cacheCreation += ru.cacheCreation || 0;
    return a;
  }, { cacheRead: 0, input: 0, cacheCreation: 0 });
  const _fleetCR = (gt.cacheRead || 0) + fleetRoot.cacheRead;
  const _fleetDen = _fleetCR + (gt.input || 0) + fleetRoot.input + (gt.cacheCreation || 0) + fleetRoot.cacheCreation;
  const fleetHit = _fleetDen ? _fleetCR / _fleetDen : hitInputSide(gt);  // hero 头条: 计费口径 (root+sub 合并); 全 0 退化旧 sub-only

  // --- meta chips ---
  const nSess = result.sessionsScanned != null ? result.sessionsScanned
              : (result.sessions ? result.sessions.length : null);
  const errN = (result.errors && result.errors.length) || 0;
  // 数据活性: 文件最新更新距今 < STALE_AFTER_S → 在动 (●实时); 旧 session 长期不动 → ⏳静止; 无 dataAgeSeconds (null/未知源) 默认当在动 (不冤枉).
  _lastDataActive = !(result.dataAgeSeconds != null && result.dataAgeSeconds > STALE_AFTER_S);
  const _ms = modeChipState();   // 顶部 mode chip 三态: 实时/静止 (数据活性) / 暂停 (live-tail 关); 来源轴在下方"数据源"下拉, 不在此重复
  document.getElementById("meta").innerHTML =
    `<span id="mode-chip" class="chip ${_ms.cls}">${_ms.text}</span>` +
    `<span class="chip">sessions <b>${nSess != null ? nSess : "?"}</b></span>` +
    `<span class="chip">spawns <b>${result.spawnsTotal != null ? result.spawnsTotal : "?"}</b></span>` +
    (errN ? `<span class="chip err">${errN} err</span>` : "");

  // --- trust 闸 → fleet 异常信号 (§8.3): 三类独立计数 (💥爆掉/⚠低命中/⏳异步未回报), 全 0 绿; 多/单 session 同一套. ---
  // 旧 ✓ all consistent 只校验 isRoot 不变量 (恒真, 无业务信息), 换成可行动的 fleet 健康信号. scanConsistency 后端仍产 (CLI 诊断用), 仅前端 banner 不再展示.
  const _ps = result.perSession || [];
  const blown = _ps.filter(isBlown).length;                         // 💥 ctx 爆掉的 session 数
  const lowHit = _ps.filter(isLowHit).length;                       // ⚠ 低命中(<60%) session 数 (独立, 与爆掉可重叠)
  const asyncN = _ps.reduce((a, r) => a + (r.asyncCount || 0), 0);  // ⏳ async_launched 未回报的 spawn 数 (飞行中/悬挂)
  const tb = document.getElementById("trust-banner");
  const nSessT = nSess != null ? nSess : _ps.length;
  const nSpT = result.spawnsTotal != null ? result.spawnsTotal : "?";
  if (blown === 0 && lowHit === 0 && asyncN === 0) {
    tb.className = "trust ok";
    tb.innerHTML = `✓ 0 异常 · <b>${nSessT}</b> sessions · <b>${nSpT}</b> spawns`;
  } else {
    const segs = [];
    if (blown)  segs.push(`<span title="ctx 压缩失败/逼爆的 session 数">💥 <b>${blown}</b> session 爆掉</span>`);
    if (lowHit) segs.push(`<span title="计费 cache 命中 <60% 的 session 数">⚠ <b>${lowHit}</b> 低命中(&lt;60%)</span>`);
    if (asyncN) segs.push(`<span title="记录时刻 async_launched 未完成的 spawn 数 (飞行中/悬挂)">⏳ <b>${asyncN}</b> 异步未回报</span>`);
    tb.className = "trust bad";
    tb.innerHTML = segs.join(" · ");
  }

  // --- hero 左: cache 经济学 ---
  // 两面板共享"活跃 session"集 = 并集(totalTokens>0 ∪ ctxPeak>0). 口径不同 (totalTokens=subagent §7;
  // ctxPeak=root 主线 §8.3), 单取任一会漏另一口径的 session → cache/context 面板计数不一致. 取并集保同集,
  // 各自口径无数据的 session 在下方分支显 — (不伪造, 计数仍一致).
  const activeRows = (result.perSession || []).filter(r => (r.totalTokens || 0) > 0 || (r.ctxPeak || 0) > 0);
  // 按 hit 降序, 头部簇 + 尾部 outlier 都露 (top4 …N more 末2)
  const withHit = activeRows
    .map(r => ({ r, hit: sessHit(r) })).sort((a, b) => (b.hit || 0) - (a.hit || 0));
  let show;
  if (withHit.length <= 7) {
    show = withHit;
  } else {
    show = withHit.slice(0, 4);
    show.push({ gap: withHit.length - 6 });
    show = show.concat(withHit.slice(-2));
  }
  let distHtml = "";
  for (const it of show) {
    if (it.gap) {
      distHtml += `<div class="dist-row dist-more" title="点 → 下方总览表查看全部 session">` +
                  `<span class="name">…${it.gap} more</span>` +
                  `<span class="bar"><span class="hit-h" style="width:90%"></span></span>` +
                  `<span class="val ok">~tail</span></div>`;
      continue;
    }
    const { r, hit } = it;
    if (hit == null) {   // 合并后仍无任何计费 token (空壳/纯 skill session, root+sub 都 0) → 显 —; 纯 root session 现有真实命中率, 不再走此分支 (2026-06-19 统一口径)
      distHtml += `<div class="dist-row is-na" data-sid="${esc(r.sid || "")}" title="${esc(sessPrefix(r) + " · 无计费 token (空壳/纯 skill) · 点进看 session 编排")}">` +
                  `<span class="name">${projName(r.project)}</span>` +
                  `<span class="bar"></span><span class="val faint">—</span></div>`;
      continue;
    }
    const hp = hit != null ? (hit * 100).toFixed(1) + "%" : "—";
    const w = hit != null ? Math.max(2, hit * 100) : 0;
    const tc = tierOf(hit, billableTotal(r));   // 合并 token 量判 empty (纯 root session totalTokens=0 但 billableTotal>0 → 不再误判 empty 灰)
    const cls = tc === "ok" ? "ok" : tc === "mid" ? "mid" : tc === "lo" ? "lo" : "";
    distHtml += `<div class="dist-row" data-sid="${esc(r.sid || "")}" title="${esc(sessPrefix(r) + " · 点进 session 编排视图")}">` +
                `<span class="name">${projName(r.project)}</span>` +
                `<span class="bar"><span class="${barClass(hit)}" style="width:${w}%"></span></span>` +
                `<span class="val ${cls}">${hp}</span></div>`;
  }
  document.getElementById("hero-cache-body").innerHTML =
    `<h2>cache 经济学 · 省钱</h2>` +
    `<p class="hint">cache 命中率（计费口径）= 合并 cacheRead / (cacheRead + input + cacheCreation) · root 主线逐 turn + subagent 全 sum（各源独立计费, 累加非重复）</p>` +
    `<div class="bigmetric"><span class="num">${fleetHit != null ? (fleetHit * 100).toFixed(1) + "%" : "—"}</span>` +
    `<span class="lab">全局 cache hit（计费口径）</span></div>` +
    `<p class="metric-sub">total ${fmt(_fleetDen)} · cacheRead ${fmt(_fleetCR)} (root ${fmt(fleetRoot.cacheRead)} + sub ${fmt(gt.cacheRead || 0)}) · 全价 input ${fmt((gt.input || 0) + fleetRoot.input + (gt.cacheCreation || 0) + fleetRoot.cacheCreation)} · hit ${pct(_fleetCR, _fleetDen)}</p>` +
    `<div class="dist">${distHtml}</div>` +
    `<p class="hint" style="margin-top:12px;margin-bottom:0;">命中率低 = 几乎全是全价 input（缓存没命中, 这块最该优化）· 色阶 ≥85% 绿 / 60–85% 琥珀 / &lt;60% 红 · — = 无计费 token（空壳/纯 skill session）</p>`;

  // --- hero 右: context 压力 (Plan 3a 已落地: 真 per-session 峰值分布) ---
  const ctxRows = activeRows
    .map(r => ({ r, peak: r.ctxPeak || 0 })).sort((a, b) => b.peak - a.peak);
  const _ctxPeaks = ctxRows.map(x => x.peak).filter(p => p > 0);   // fleet 峰值只取真峰 (>0); 全 — 时不伪造
  const fleetCtxPeak = _ctxPeaks.length ? Math.max(..._ctxPeaks) : null;
  let ctxShow;
  if (ctxRows.length <= 7) {
    ctxShow = ctxRows;
  } else {
    ctxShow = ctxRows.slice(0, 4);
    ctxShow.push({ gap: ctxRows.length - 6 });
    ctxShow = ctxShow.concat(ctxRows.slice(-2));
  }
  let ctxDist = "";
  for (const it of ctxShow) {
    if (it.gap) {
      ctxDist += `<div class="dist-row dist-more" title="点 → 下方总览表查看全部 session">` +
                 `<span class="name">…${it.gap} more</span>` +
                 `<span class="bar"><span class="ctx-fill" style="width:60%"></span></span>` +
                 `<span class="val">~mid</span></div>`;
      continue;
    }
    const { r, peak } = it;
    if (peak <= 0) {   // 活跃但 root 主线无 usage 抽样 (token 全在 subagent) → context 面板无数据, 显 — 与 cache 面板计数对齐
      ctxDist += `<div class="dist-row is-na" data-sid="${esc(r.sid || "")}" title="${esc(sessPrefix(r) + " · 无 root 主线 ctx 抽样 · token 全在 subagent · 点进看 session 编排")}">` +
                 `<span class="name">${projName(r.project)}</span>` +
                 `<span class="bar"></span><span class="val faint">—</span></div>`;
      continue;
    }
    const w = fleetCtxPeak ? Math.max(2, peak / fleetCtxPeak * 100) : 0;
    const blown = r.ctxLimitErrors && r.ctxLimitErrors.count > 0;   // §8.3 💥 爆掉 (优先级高于 ⚠)
    const warn = !blown && peak > CTX_LARGE;
    const cls = blown ? "blown" : (warn ? "mid" : "");
    const glyph = blown ? "💥 " : (warn ? "⚠ " : "");
    const fillCls = blown ? "ctx-fill-blown" : (warn ? "ctx-fill-warn" : "");
    const drillHint = "点进 session 编排视图";
    const detail = blown
      ? `💥 压缩失败爆掉 (peak ${fmt(peak)}; ${r.ctxLimitErrors.count}× ${String(r.ctxLimitErrors.sample || "").slice(0, 70)}) · ${drillHint}`
      : drillHint;
    const title = sessPrefix(r) + " · " + detail;   // 前置 session 区分前缀 (project全名+sid+时长+spawns); 下方整体 esc
    const out = (r.grandTotal && r.grandTotal.output) || 0;   // session 总 output (伴随: 占生成期窗口, 不进 ctxPeak)
    ctxDist += `<div class="dist-row" data-sid="${esc(r.sid || "")}" title="${esc(title)}">` +
               `<span class="name">${projName(r.project)}</span>` +
               `<span class="bar"><span class="ctx-fill ${fillCls}" style="width:${w}%"></span></span>` +
               `<span class="val ${cls}">${glyph}${fmt(peak)}<span class="ctx-out">out ${fmtK(out)}</span></span></div>`;
  }
  const ctxBody = activeRows.length
    ? (fleetCtxPeak != null
        ? `<div class="bigmetric"><span class="num ctx-num">${fmt(fleetCtxPeak)}</span>` +
          `<span class="lab">全局最大 ctx 峰值（per-session max）</span></div>` +
          `<p class="metric-sub">per-turn 上下文 = input + cacheCreation + cacheRead（prompt 侧, 缓存杠杆）; output 单列, 占生成期窗口但不进峰值</p>`
        : `<p class="hint">本总览活跃 session 均无 root 主线 usage 抽样（token 全在 subagent）—— context 峰值待 root 主线 turn。</p>`) +
      `<div class="dist">${ctxDist}</div>` +
      `<p class="hint" style="margin-top:12px;margin-bottom:0;">⚠ = 大绝对上下文（&gt; ${fmt(CTX_LARGE)}，仅提示绝对大小, 非逼近模型上限） · 💥 = 压缩失败爆掉（context window limit API Error；事件驱动, 非逼近 limit 也可能爆） · — = 该 session 无此面板口径数据</p>`
    : `<p class="hint">本总览无活跃 session（totalTokens 与 ctxPeak 全 0）。</p>`;
  document.getElementById("hero-context-body").innerHTML =
    `<h2>context 压力 · 省空间</h2>` +
    `<p class="hint">per-session context 峰值（各 turn 的最大值）</p>` +
    ctxBody;

  // --- fleet 表 (8 列, 按 total 降序; 空壳沉底, 淡灰不告警) ---
  const tbody = document.querySelector("#fleet-table tbody");
  tbody.innerHTML = "";
  const sorted = sortRows(result.perSession || []);
  // 单行构建抽成函数 (gen-tag + gen-group 分派共用); 8 列 → gen-head colspan=8.
  function fleetRow(r) {
    const b = billable(r);                                  // 合并 root 主线 + subagent 三桶 (== cache 面板口径)
    const bt = b.cacheRead + b.input + b.cacheCreation;     // 合并计费总量 (纯 root session 不再显 0)
    const hit = sessHit(r);                                 // 合并命中率 (与 hero/cache 面板一致)
    const fullInput = b.input + b.cacheCreation;            // 全价 input (省钱靶子, 合并)
    const tc = tierOf(hit, bt);
    const spotlight = tc === "mid" || tc === "lo";
    const tr = document.createElement("tr");
    if (spotlight) tr.className = "spotlight";
    else if (tc === "empty") tr.className = "empty";
    tr.dataset.sid = r.sid || "";
    tr.dataset.gen = r.generationId || "";                  // Phase 3: 续接 generationId (== sid 则无 carrier, gen-tag 不显)
    tr.style.cursor = "pointer";
    tr.addEventListener("click", () => drillSession(r.sid));
    let cacheCell;
    if (tc === "empty" || tc === "na") {
      cacheCell = `<span class="faint">—</span>`;
    } else {
      const cw = Math.max(2, hit * 100);
      cacheCell = `<span class="cache"><span class="cbar"><span class="${barClass(hit)}" style="width:${cw}%"></span></span>` +
                  `<span class="${tc}">${(hit * 100).toFixed(1)}%</span></span>`;
    }
    const tag = spotlight ? `<span class="spot-tag">outlier</span>`
              : (tc === "empty" ? `<span class="empty-tag">空壳</span>` : "");
    // Phase 3 跨 session 续接 (§10.1): generationId != sid (carrier 把多 session 缝成同 generation) → 显 ⟿ 续接 tag
    const genTag = (r.generationId && r.generationId !== r.sid)
      ? `<span class="gen-tag" title="跨 session 续接 generation ${esc(r.generationId)}">⟿ ${esc(String(r.generationId).slice(0, 8))}</span>`
      : "";
    tr.innerHTML =
      `<td class="sess">${projName(r.project)} <span class="sid">${esc((r.sid || "?").slice(0, 8))}</span>${tag}${genTag}</td>` +
      `<td class="num">${r.spawns != null ? r.spawns : 0}</td>` +
      `<td class="num">${fmtDur(r.durationS)}</td>` +
      `<td class="num">${fmt(bt)}${budgetChip((_gensById[r.generationId] || {}).budgetState)}</td>` +
      `<td class="num">${cacheCell}</td>` +
      `<td class="num">${fmt(fullInput)}</td>` +
      `<td class="num">${ctxCell(r.ctxPeak, r.ctxLimitErrors)}</td>` +
      `<td class="num">${healthCell(r)}</td>`;
    return tr;
  }
  // Phase 3 gen-group (默认关 → 逐行平铺, 今天行为逐字不变): 勾选则按 generationId 分桶,
  // multiSession (>1 成员) 桶前插组头行 (generationId + 成员数 + 卷起 total, 取 result.generations).
  const _gensById = {};
  for (const g of (result.generations || [])) _gensById[g.generationId] = g;
  const genGroupOn = !!(document.getElementById("gen-group") && document.getElementById("gen-group").checked);
  if (genGroupOn) {
    const buckets = {}, order = [];
    for (const r of sorted) {
      const gid = r.generationId || r.sid;
      if (!buckets[gid]) { buckets[gid] = []; order.push(gid); }
      buckets[gid].push(r);
    }
    for (const gid of order) {
      const rows = buckets[gid], g = _gensById[gid];
      if (g && g.multiSession && rows.length > 1) {
        const gh = document.createElement("tr");
        gh.className = "gen-head";
        gh.innerHTML = `<td colspan="8"><span class="gen-tag">⟿ ${esc(gid.slice(0, 8))}</span> ` +
          `<span class="faint">跨 session 续接 · ${g.sessionsN} session · 卷起 ${fmt((g.grandTotal || {}).total || 0)} token</span>${budgetChip(g.budgetState)}</td>`;
        tbody.appendChild(gh);
      }
      for (const r of rows) tbody.appendChild(fleetRow(r));
    }
  } else {
    for (const r of sorted) tbody.appendChild(fleetRow(r));
  }
  // 合计 footer
  const ft = document.createElement("tr");
  ft.className = "total-row";
  ft.innerHTML =
    `<td class="sess">合计</td>` +
    `<td class="num">${fmt(result.spawnsTotal)}</td><td class="num">—</td>` +
    `<td class="num">${fmt(_fleetDen)}</td>` +
    `<td class="num">${fleetHit != null ? (fleetHit * 100).toFixed(1) + "%" : "—"}</td>` +
    `<td class="num">${fmt((gt.input || 0) + fleetRoot.input + (gt.cacheCreation || 0) + fleetRoot.cacheCreation)}</td>` +
    `<td class="num"><span class="faint">—</span></td>` +
    `<td class="num">${(blown + lowHit + asyncN) > 0 ? '<span class="lo badge" title="' + blown + ' 爆掉 · ' + lowHit + ' 低命中 · ' + asyncN + ' 异步">✗ ' + (blown + lowHit + asyncN) + '</span>' : '<span class="ok badge">✓</span>'}</td>`;
  tbody.appendChild(ft);
  updateSortHeader();

  // --- skill 活跃度切面 (§8.11): 常驻表 (零 token 能力画像), 点行 → inline 展开 session/spawn/turn ---
  renderSkillActivity(result);
}

// === session view (§8.6: spawn 时间轴 + twin-L2 + outlier 定位) ===
const TYPE_COLOR = {
  "Explore": "var(--blue)", "general-purpose": "var(--green)", "Plan": "var(--purple)",
  "claude": "var(--amber)", "claude-code-guide": "#79c0ff", "unknown": "var(--faint)"
};
// === skill 视角 §8.11 (常驻活跃度表 + 点行 inline 展开 session/spawn/turn) ===
function renderSkillActivity(result) {
  const sb = document.querySelector("#skill-table tbody");
  if (!sb) return;
  sb.innerHTML = "";
  for (const s of (result.bySkill || [])) {
    const tr = document.createElement("tr");
    tr.dataset.skill = s.skillName || "?";
    tr.title = "点行 → 展开该 skill 出现在哪些 session / spawn / turn";
    tr.innerHTML =
      `<td>${esc(s.skillName || "?")}</td>` +
      `<td class="num">${s.calls != null ? s.calls : "?"}</td>` +
      `<td class="num">${s.sessions != null ? s.sessions : "?"}</td>` +
      `<td class="num">${s.spawns != null ? s.spawns : "—"}</td>`;
    tr.addEventListener("click", () => toggleSkillDetail(s, result, tr));
    sb.appendChild(tr);
  }
  // live-tail 2s 重渲染会清空 tbody 抹掉展开行 → 保留 _skillExpandedName, 重建后自动重展开 (跨 poll 不闪退, 且用新 result 刷新 turns).
  if (_skillExpandedName) {
    const tr = [...sb.querySelectorAll("tr")].find(t => t.dataset.skill === _skillExpandedName);
    const s = (result.bySkill || []).find(x => x.skillName === _skillExpandedName);
    if (tr && s) toggleSkillDetail(s, result, tr);   // 复用 toggle: 单展开 / sel / 事件绑定一致
    else _skillExpandedName = null;                   // skill 已不在新结果 → 清状态 (下次不误展开)
  }
}

// 点 skill 行 → 在该 <tr> 后 toggle 一个 .skill-detail 展开行 (master-detail inline). 再点同行收起; 切换他行时单展开 (同时只一个, 避免堆叠混乱).
// 展开内容消费 turns[] (每项 {sessionId,agentId,agentType,turn}): 按 sessionId 分组, 每组一个可点 session chip (→ drillSession) + 其下调用清单 (agentType · turn 号; root 直发 agentId=null/agentType=orchestrator 显 "root").
function toggleSkillDetail(skill, result, tr) {
  const name = skill.skillName || "?";
  const nxt = tr.nextElementSibling;
  if (nxt && nxt.classList.contains("skill-detail") && nxt.dataset.skill === name) {
    nxt.remove();          // 再点已展开行 → 收起
    tr.classList.remove("sel");
    _skillExpandedName = null;   // 收起 → 清状态 (live 重渲染不再误展开)
    return;
  }
  // 单展开: 清其他展开行, 标记当前行为选中
  document.querySelectorAll("#skill-table tbody tr").forEach(t => {
    if (t.classList.contains("skill-detail")) t.remove();
    else t.classList.toggle("sel", t === tr);
  });
  tr.classList.add("sel");

  const turns = skill.turns || [];
  const bySid = {};
  for (const r of (result.perSession || [])) bySid[r.sid] = r;
  let body;
  if (!turns.length) {
    body = `<div class="bl-empty">该 skill 无 turn 锚点 (turns 为空 — live 未补全或 root 主线无 callerTurn)</div>`;
  } else {
    const order = [], groups = {};
    for (const t of turns) {
      const sid = t.sessionId || "(无 session 锚点)";
      if (!(sid in groups)) { groups[sid] = []; order.push(sid); }
      groups[sid].push(t);
    }
    const blocks = order.map(sid => {
      const r = bySid[sid];
      const tail = r ? ` · ${fmt(billableTotal(r))} tok` : "";   // 合并 root+sub 计费总量 (== fleet 表 total 列; 纯 root 不显 0)
      const lbl = r ? projName(r.project) + " · " + String(sid).slice(0, 8) : String(sid).slice(0, 12);
      const calls = groups[sid].map(t => {
        const at = esc(t.agentType || "?");
        // spawn 序号 spawnIdx = by_skill 后端复刻 session 页 segs 时序 (start=ts-dur 升序的 #i); "#N" 与时间轴竖线/agents 行徽章同号. root 直发/未补全 → 无号.
        const who = (t.agentId == null) ? "root" : ("spawn " + String(t.agentId).slice(0, 8));
        const turnNum = t.turn != null ? t.turn : "?";
        const ti = t.turn != null ? t.turn : "";                 // data-turn (空 = 无 turn 锚)
        const ds = esc(t.sessionId || ""), da = esc(t.agentId || "");
        // #N / turn 都可点 → 经 session 中转保栈. #N (target=spawn) → spawn 详情全貌 (level3); turn (target=turn) → turn 原文 (level4, 经 spawn 详情中转渲染 level3 保返回栈). root 直发 (da 空) → drillRoot/drillTurn("root").
        const idxBadge = (t.spawnIdx != null)
          ? `<span class="sd-idx sd-go" data-sid="${ds}" data-agent="${da}" data-turn="${ti}" data-target="spawn" title="${who} · session 时间轴同序号 · 点进 spawn 详情">#${t.spawnIdx}</span> ` : "";
        return `<span class="sd-call" title="${who} · agentType ${at}">${idxBadge}${at} `
          + `<span class="sd-turn sd-go" data-sid="${ds}" data-agent="${da}" data-turn="${ti}" data-target="turn" title="${who} · turn ${turnNum} · 点进 turn 原文">turn ${turnNum}</span></span>`;
      }).join("");
      return `<div class="sd-group"><span class="bl-sess" data-sid="${esc(sid)}" title="点 → session 编排视图">${esc(lbl)}${tail} →</span><div class="sd-calls">${calls}</div></div>`;
    }).join("");
    body = `<div class="bl-title">「${esc(name)}」出现在 ${order.length} 个 session · ${turns.length} 次调用 · 点 session chip → 编排视图</div>${blocks}`;
  }
  const det = document.createElement("tr");
  det.className = "skill-detail";
  det.dataset.skill = name;
  det.innerHTML = `<td colspan="4"><div class="skill-detail-box">${body}</div></td>`;
  tr.after(det);
  _skillExpandedName = name;   // 展开成功 → 记名 (live 重渲染时据此重展开, 用新 result 刷 turns)
  det.querySelectorAll(".bl-sess").forEach(el =>
    el.addEventListener("click", () => drillSession(el.dataset.sid)));
  det.querySelectorAll(".sd-go").forEach(el =>               // #N → spawn 详情; turn → turn 原文 (经 session 中转保栈)
    el.addEventListener("click", ev => { ev.stopPropagation(); _drillFromSkill(el.dataset.sid, el.dataset.agent || null, el.dataset.turn, el.dataset.target || "spawn"); }));
}

function typeColor(t) { return TYPE_COLOR[t] || "var(--blue)"; }

function _drillFromSkill(sid, agentId, turn, target) {
  // 从 fleet skill 展开钻 spawn 详情 / turn 原文. drill* 靠全局 _sessionCtx.sid, 而 fleet 页无 drill session →
  // 先 drillSession (渲染 level2 + 设 _sessionCtx, 含 sessionHit/idxByAgent 非降级), 栈 fleet→session 正确; 再据 target:
  //   "spawn" → spawn 详情 (level3 全貌); "turn" → turn 原文 (level4, 经 spawn 详情中转渲染 level3, 返回栈 spawn→session→fleet 逐级).
  if (!sid) return;
  fetch("/api/session/" + encodeURIComponent(sid)).then(r => r.json()).then(d => {
    if (d.error) { alert("session load failed: " + d.error); return; }
    showSession(d, sid);
    const ti = (turn !== undefined && turn !== null && turn !== "") ? Number(turn) : undefined;
    const root = (agentId == null || agentId === "");
    if (target === "turn" && ti !== undefined) {           // turn 原文: spawn 详情 focus 该 turn → 钻 level4 (返回 spawn 时定位该 turn)
      if (root) drillRoot(ti, () => drillTurn("root", ti));
      else drillSpawn(agentId, ti, () => drillTurn(agentId, ti));
    } else {                                                // spawn 全貌 (无 focus, 看整个 spawn)
      if (root) drillRoot();
      else drillSpawn(agentId);
    }
  }).catch(e => alert("session fetch error: " + e));
}

function drillSession(sid) {
  if (!sid) return;
  // 不在 topbar 注 loading chip: showSession 会切走 fleet-view, chip 留 topbar 成残留假"loading"状态 (诚实底线); 本地 fetch 快, 视图切换即反馈.
  fetch("/api/session/" + encodeURIComponent(sid)).then(r => r.json()).then(d => {
    if (d.error) { alert("session load failed: " + d.error); return; }
    showSession(d, sid);
  }).catch(e => alert("session fetch error: " + e));
}

function showSession(d, sid) {
  const gt = d.grandTotal || {};
  // session 级 total/hit 走合并口径 (sub grandTotal + root 主线 rootContext.sum), == fleet 表/hero 同口径 (2026-06-19).
  // 纯 root session (grandTotal 全 0) 不再 total 显 0 / cache hit 显 —; 与主面板一致.
  // 注意: _sessionCtx.sessionHit (spawn 比对基线, 见下) 仍用 hitInputSide(gt) sub-only — spawn 是 subagent,
  // 它的 hit 只该和 subagent 均值比, 不该和 root 主线灌高的合并均值比 (否则每个 spawn 都像低 outlier).
  const _sess = { grandTotal: gt, rootUsage: (d.rootContext || {}).sum || {} };
  const sessTot = billableTotal(_sess);   // session 计费总量 (合并)
  const sessHitVal = sessHit(_sess);      // session 命中率 (合并, == fleet 表 cache 列)
  const spawns = d.callChains || [];
  // 健康信号分轨 (2026-06-19): async_launched = run_in_background 后台 agent, 跑完经 task-notification 回报 root,
  // 不占 root wall-clock 但消耗 token —— 非 failed (旧码 success=False 误判). 真"失败" = status 既非 completed 也非 async_launched.
  // spawns 已按 timestamp 排序 (build_topology:206), filter 保序.
  const asyncSpawns = spawns.filter(s => s.status === "async_launched");
  const failSpawns = spawns.filter(s => s.status && s.status !== "completed" && s.status !== "async_launched");
  const asyncN = asyncSpawns.length, failN = failSpawns.length;
  // gantt 段: start ≈ ts - durationMs, end = ts (v1 近似, §8.6 L546; per-spawn 精确 start 须 tool_use→tool_result 配对, 后续细化)
  const segs = spawns.map(s => {
    const ts = Date.parse(s.timestamp || "");
    const dur = s.durationMs || 0;
    return { type: s.subagentType || "unknown", start: ts - dur, end: ts || (ts - dur),
             hit: hitInputSide(s.tokens || {}), tokens: s.tokens || {},
             success: s.success, model: s.resolvedModel,
             agentId: s.spawnedAgentId || "", status: s.status,
             toolErr: s.toolErrorCount || 0 };   // §8.6 ✗ tool 失败 (该 spawn 内部 is_error 计数; status: 异步标/gantt 排除 + toolErr: ✗ 标 — 两轨独立, 不并 success/fail)
  }).filter(s => !isNaN(s.start) && s.end >= s.start);
  // segs 按 wall-clock 时序定 #i —— agents 行徽章 / 时间轴竖线 tooltip / skill 切面 共用同一时序序号, 全局可互指.
  // 同 timestamp 并发的相对顺序任意 (Array.sort 稳定, 用户认可 "同一时刻编号可随机"); callChains 已升序, 显式 sort 保证不依赖上游顺序.
  segs.sort((a, b) => a.start - b.start);
  // 异步 spawn durationMs=None → 0 宽 (start==end=ack ts); 时间轴不当宽度段画, 但其 timestamp=启动 ack 是真实时刻.
  // 用户: "异步什么时候启动的, 划道鲜明的线" → 时间轴补 async 启动竖线 (无宽度 marker). 故轴范围须 sync∪async 联合求,
  // 否则异步 marker 落在 sync [tmin,tmax] 之外. 运行时长确拿不到 (完成走 task-notification 不回写) → marker 诚实标"运行时长未知".
  const ganttSegs = segs.filter(s => s.status !== "async_launched");
  const asyncSegs = segs.filter(s => s.status === "async_launched");
  // D1/D2: root 主线 turn 点 (rootContext.samples, 带 ts+turnIndex). 时间跨度并入 root 首/末 ts →
  // root 在 subagent 包络外的活动不再被裁 (D2: tmin/tmax = root∪subagent 并集).
  const rootTurns = ((d.rootContext || {}).samples || []).filter(s => s && s.ts);
  const rootTs = rootTurns.map(s => Date.parse(s.ts)).filter(n => !isNaN(n));
  const tlPts = [];
  ganttSegs.forEach(s => { tlPts.push(s.start, s.end); });
  asyncSegs.forEach(s => { tlPts.push(s.end); });   // async start==end==ack ts
  rootTs.forEach(t => tlPts.push(t));               // D2: root turn ts 并入跨度
  const tmin = tlPts.length ? Math.min(...tlPts) : 0;
  const tmax = tlPts.length ? Math.max(...tlPts) : 0;
  const span = (tmax - tmin) || 1;
  _sessionCtx = { sid: sid, sessionHit: hitInputSide(gt) };   // showSpawn 读: spawn hit vs session 均值 (§8.6 头)
  ganttSegs.sort((a, b) => a.start - b.start);

  // 时间轴
  let ganttHtml = "";
  ganttSegs.forEach((s, i) => {
    const left = (s.start - tmin) / span * 100;
    const w = Math.max(0.6, (s.end - s.start) / span * 100);
    const prev = i > 0 ? ganttSegs[i - 1] : null;
    if (prev && (s.start - prev.end) > 10 * 60 * 1000) {
      const gapMin = Math.round((s.start - prev.end) / 60000);
      const gapLeft = ((prev.end + s.start) / 2 - tmin) / span * 100;  // gap 中点定位 (translateX -50% 居中)
      // D3: 间隙含 root turn → "含 N root turn" (root 在活动, 非 idle); 仅真空 → "空闲 Nm".
      const rn = rootTs.filter(t => t > prev.end && t < s.start).length;
      const gTxt = rn > 0 ? `含 ${rn} root turn` : `空闲 ${gapMin}m`;
      ganttHtml += `<span class="gantt-gap${rn > 0 ? ' has-root' : ''}" style="left:${gapLeft}%" title="${gTxt}">${gTxt}</span>`;
    }
    const ol = (s.hit != null && s.hit < 0.6) ? " outlier" : "";
    ganttHtml += `<span class="gantt-seg${ol}" data-agentid="${esc(s.agentId)}" data-agentids="${esc(s.agentId)}" style="left:${left}%;width:${w}%;background:${typeColor(s.type)};"`
      + ` title="${esc(s.type)} · #${segs.indexOf(s)} · hit ${s.hit != null ? (s.hit * 100).toFixed(0) + '%' : '—'} · dur ${fmtDurMs(s.end - s.start)} · 悬停高亮对应 agents 行 · 点进看详情"></span>`;
  });
  // async 启动竖线: 多个 async 常同 turn 并发启动 (run_in_background 扇出) → 同 timestamp 折成 1 线 + ×N 计数, 全量可追溯.
  // (用户: "25 异步怎么只 9 线" → 并发同刻重叠非丢失; 实测 7d4eb5c6: 25 async 跨 11 刻, 簇 [6,5,2,2,2,2,2,1]). 点线进 spawn 详情 (首个; 全量见 agents 面板).
  // 同名并发消歧 (用户: "同名的也不知道对应面板里哪个"): tooltip 给 roster 序号 #i (与 agents 行首徽章对齐), 悬停再高亮对应行.
  const asyncByMoment = {};
  asyncSegs.forEach(s => { const k = s.end; (asyncByMoment[k] = asyncByMoment[k] || []).push(s); });
  Object.keys(asyncByMoment).forEach(k => {
    const grp = asyncByMoment[k], n = grp.length;
    const left = (Number(k) - tmin) / span * 100;
    const multi = n > 1;
    const idxStr = grp.map(g => "#" + segs.indexOf(g)).join(",");   // roster 序号 = segs 位置 (与 agents 行 data-idx 一致; asyncSegs 是 segs.filter, 同引用)
    let typeStr;
    if (!multi) {
      typeStr = `${esc(grp[0].type)} 异步启动`;
    } else {
      // 类型去重 + 计数: 全同名 → "6× Explore"; 混名 → "6 个 (Explore×4, general-purpose×2)" — 同名时序号是唯一区分
      const tc = {};
      grp.forEach(g => { tc[g.type] = (tc[g.type] || 0) + 1; });
      const ent = Object.entries(tc).sort((a, b) => b[1] - a[1]);
      typeStr = ent.length === 1
        ? `${n}× ${esc(ent[0][0])} 并发启动`
        : `${n} 个并发 (${ent.map(([t, c]) => `${esc(t)}×${c}`).slice(0, 3).join(", ")}${ent.length > 3 ? "…" : ""})`;
    }
    const head = `${typeStr} @ 此刻 · agents 面板 ${idxStr}`;
    ganttHtml += `<span class="gantt-async${multi ? " multi" : ""}" data-agentid="${esc(grp[0].agentId)}" data-agentids="${esc(grp.map(g => g.agentId).join(","))}" style="left:${left}%"`
      + ` title="${head} · 运行时长未知 (run_in_background 完成走 task-notification 不回写) · 悬停高亮对应 agents 行 · 点进看详情 (首个; 全量见 agents 面板)">`
      + (multi ? `<span class="gantt-async-n">×${n}</span>` : "") + `</span>`;
  });

  // D1/D12: root 主线 turn = 时间轴上方 root lane 的离散紫点. sample 只有单点 ts (无 duration/end) → 画段会把
  // "root 等 subagent 的空档"误算成 root 持续活动 (不诚实) → 故画离散点, 只表达"此刻 root 执行了一个 turn".
  // 点 x = s.ts (真实时刻); data-turn = s.turnIndex (D15: 与 agent_turn_raw 同空间, 绝非 sample.i 去重位次).
  // peak turn (samples 里 ctx 最大者) 点加琥珀环, 与 sparkline peak 呼应. 点 → drillRoot 进 root 详情 (定位 turn).
  if (rootTurns.length) {
    const peakIdx = rootTurns.reduce((m, s, i, a) => ((s.ctx || 0) > (a[m].ctx || 0) ? i : m), 0);
    rootTurns.forEach((s, i) => {
      const t = Date.parse(s.ts); if (isNaN(t)) return;
      const left = (t - tmin) / span * 100;
      const isPeak = (i === peakIdx);
      // DR7: tip 带 turn 序号 (#${ti}, 用户要的) + 时间 = 相对 session 起点偏移 fmtDurMs(t-tmin),
      // 与 gantt-axis 0 起点刻度 (line ~778: 0 / fmtDurMs(span/2) / fmtDurMs(span)) 同源同口径 —— 不再显绝对 HH:MM:SS.
      const ti = (s.turnIndex != null) ? s.turnIndex : '';
      const tip = `root turn #${ti} · +${fmtDurMs(t - tmin)} · ctx ${fmt(s.ctx || 0)}${isPeak ? ' · peak' : ''} · 点进 root 详情`;
      ganttHtml += `<span class="root-dot${isPeak ? ' peak' : ''}" data-agentid="root" data-turn="${ti}" style="left:${left}%" title="${tip}"></span>`;
    });
  }

  // cache 半边: 书挡视图 (镜像总览页 hero, app.js:180-215) —— 全 segs 按 hit 降序, ≤7 全显, >7 头4+…N more+末2.
  // 用户: "像总览页那样, 列个最高 + more + 列个低的" —— 光列最低的没参照系 (不知正常/最好多少); 两端书挡给锚点.
  // .dist-row 样式 == hero (视觉与总览页一致); data-agentid 行点 → spawn 详情; …N more → 滚闪下方 agents 面板 (== hero more→fleet 表).
  const cacheSorted = segs.map(s => ({ s, hit: s.hit })).sort((a, b) => {
    if (a.hit == null && b.hit == null) return 0;
    if (a.hit == null) return 1;          // 无命中沉底
    if (b.hit == null) return -1;
    return b.hit - a.hit;                 // 降序: 最高在前
  });
  let cacheShow;
  if (cacheSorted.length <= 7) {
    cacheShow = cacheSorted;
  } else {
    cacheShow = cacheSorted.slice(0, 4);
    cacheShow.push({ gap: cacheSorted.length - 6 });
    cacheShow = cacheShow.concat(cacheSorted.slice(-2));
  }
  let cacheBars = "";
  if (!cacheSorted.length) {
    cacheBars = '<span class="faint">无 spawn (零 depth-2 嵌套).</span>';
  } else {
    for (const it of cacheShow) {
      if (it.gap) {
        cacheBars += `<div class="dist-row dist-more" title="点 → 滚到下方 agents 面板查看全部 spawn">`
          + `<span class="name">…${it.gap} more</span>`
          + `<span class="bar"><span class="hit-h" style="width:90%"></span></span>`
          + `<span class="val ok">~mid</span></div>`;
        continue;
      }
      const { s } = it;
      const h = s.hit, w = h != null ? Math.max(2, h * 100) : 0;
      const hp = h != null ? (h * 100).toFixed(0) + '%' : '—';
      const cls = h != null ? (h >= 0.85 ? "ok" : h >= 0.6 ? "mid" : "lo") : "faint";
      const am = s.status === "async_launched";
      cacheBars += `<div class="dist-row" data-agentid="${esc(s.agentId)}" title="${esc(s.type)} · #${segs.indexOf(s)} · hit ${hp} · 点进看详情">`
        + `<span class="name">${esc(s.type)} <span class="row-idx">#${segs.indexOf(s)}</span>${am ? ' <span class="async-tag">异步</span>' : ''}</span>`
        + `<span class="bar"><span class="${barClass(h)}" style="width:${w}%"></span></span>`
        + `<span class="val ${cls}">${hp}</span></div>`;
    }
  }

  // twin 右: root context 曲线 (内联 SVG, 无 CDN)
  const rc = d.rootContext || {};
  const samples = rc.samples || [];
  const peak = rc.peak || 0;
  let svgBlock;
  if (samples.length) {
    const W = 320, H = 96, n = samples.length;
    const maxCtx = Math.max(...samples.map(s => s.ctx), 1);
    // D15: x 用真实 ts (与 gantt 时间轴同 tmin/span 对齐), 不再用 turn 序号 i (序号丢时刻且与 agent_turn_raw 不同空间).
    // 逐点透明命中圆 (r=6) → 点 drillTurn("root", s.turnIndex) 进 turn 原文 (D15: turnIndex, 非 sample.i).
    const spanOk = span > 0;
    const px = s => spanOk ? (Date.parse(s.ts) - tmin) / span * W : 0;
    const py = s => (H - (s.ctx || 0) / maxCtx * H).toFixed(1);
    const pts = samples.map(s => `${(isNaN(Date.parse(s.ts)) ? 0 : px(s)).toFixed(1)},${py(s)}`).join(" ");
    const peakSample = samples.reduce((m, s) => ((s.ctx || 0) > ((m && m.ctx) || 0) ? s : m), samples[0]);
    const peakX = (peakSample && !isNaN(Date.parse(peakSample.ts))) ? px(peakSample).toFixed(1) : null;
    const peakY = peak ? (H - peak / maxCtx * H).toFixed(1) : null;
    const peakTurnIdx = (peakSample && peakSample.turnIndex != null) ? peakSample.turnIndex : null;
    const hits = samples.map(s => {
      if (s.turnIndex == null || isNaN(Date.parse(s.ts))) return "";
      return `<circle class="spark-pt" data-turn="${s.turnIndex}" cx="${px(s).toFixed(1)}" cy="${py(s)}" r="6" fill="transparent"/>`;
    }).join("");
    svgBlock = `<svg class="spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">`
      + `<polyline points="0,${H} ${pts} ${W},${H}" fill="rgba(88,166,255,.12)" stroke="none"/>`
      + `<polyline points="${pts}" fill="none" stroke="var(--blue)" stroke-width="1.2"/>`
      + (peakX ? `<circle cx="${peakX}" cy="${peakY}" r="2.5" fill="var(--amber)"/>` : "")
      + hits
      + `</svg>`
      + `<div class="spark-cap">peak ${fmt(peak)}${peakTurnIdx != null ? ` @ root turn ${peakTurnIdx}` : ''} · ${n} turns · grow→peak · 点曲线看原文`
      + `${peak && peak > CTX_LARGE ? ' →<b class="lo"> 大上下文</b>' : ' →drop'}</div>`;
  } else {
    svgBlock = `<p class="faint">该 session 无 root 主线 usage 抽样 (rootContext 空 · 无 usage 记录).</p>`;
  }

  // outlier 定位 callout
  const hitSegs = segs.filter(s => s.hit != null);
  const olSpawn = hitSegs.length ? hitSegs.slice().sort((a, b) => a.hit - b.hit)[0] : null;
  const olHtml = olSpawn
    ? `<div class="callout"><b>outlier 定位</b> · ${esc(olSpawn.type)} 命中 ${(olSpawn.hit * 100).toFixed(0)}% · `
      + `fresh input ${fmt((olSpawn.tokens.input || 0) + (olSpawn.tokens.cacheCreation || 0))} · `
      + `序列位置 ${segs.indexOf(olSpawn) + 1}/${segs.length}</div>` : "";

  // agents 面板: 全 spawn 花名册 (sync + 异步 + 失败 统一一行) —— 吸收原异步专列 (消重).
  // 用户: "不只异步 agent 需要这一行, 其他 agent 也需要" + cache 书挡 …N more 跳到这里. 全可点 → spawn 详情.
  let agentsHtml = "";
  if (segs.length) {
    const rows = segs.map((s, i) => {                       // i = roster 序号 (segs 位置), 时间轴竖线 tooltip 用同口径 #i 回指本行
      const tk = s.tokens || {};
      const bt = (tk.cacheRead || 0) + (tk.input || 0) + (tk.cacheCreation || 0);
      const hp = s.hit != null ? (s.hit * 100).toFixed(0) + '%' : '—';
      let tag = '<span class="agent-tag done">完成</span>', rowCls = "";
      if (s.status === "async_launched") { tag = '<span class="agent-tag async">异步</span>'; rowCls = " async"; }
      else if (s.status && s.status !== "completed") { tag = `<span class="agent-tag fail">${esc(s.status)}</span>`; rowCls = " fail"; }
      return `<div class="agent-row${rowCls}" data-agentid="${esc(s.agentId)}" data-idx="${i}" data-agentids="${esc(s.agentId)}" title="点进看逐 turn 详情">`
        + tag
        + `<span class="agent-idx">#${i}</span>`
        + `<span class="agent-type">${esc(s.type)}</span>`
        + `<span class="agent-meta">hit ${hp} · cacheRead ${fmt(tk.cacheRead || 0)} · billable ${fmt(bt)}</span>`
        + `<span class="agent-model">${s.model ? esc(s.model) : ""}</span>`   // 始终占位 (col5): 保 6 列对齐, 空=不可见
        + (s.toolErr ? `<span class="tool-err" title="${s.toolErr} 个 tool_result is_error (Bash 非零退出/Edit 未命中等 provider 层失败; 与 spawn status 分轨, 不并 success/fail) · 点进逐 turn ✗ 定位">✗${s.toolErr}</span>` : `<span></span>`)   // col6: ✗ 指向元凶 spawn (status=completed 仍可标, 证 is_error≠status)
        + `</div>`;
    }).join("");
    agentsHtml = `<section class="card" id="agents-panel"><div class="sec-head"><h2>agents · <b>${segs.length}</b> 个 spawn</h2>`
      + `<span class="note">全 spawn 花名册 (sync + 异步 + 失败) · 异步 = run_in_background 后台 (无 wall-clock, 见时间轴竖线) · 失败 = status 既非 completed 也非 async · 点行看详情</span></div>`
      + `<div class="agent-list">${rows}</div></section>`;
  }

  // segs 的 agentId → 时序 #i 映射: skill 切面 / 拓扑 / 详情页标题 共用, 无条件建 (无 skill session 也要供拓扑用, 否则 ReferenceError).
  const idxByAgent = {}; segs.forEach((s, i) => { if (s.agentId) idxByAgent[s.agentId] = i; });
  const typeByAgent = {}; segs.forEach(s => { if (s.agentId) typeByAgent[s.agentId] = s.type; });   // D8: skill turn chip #i 带 agent 类型名
  if (_sessionCtx) _sessionCtx.idxByAgent = idxByAgent;  // 挂进 ctx, 供 showSpawn (顶层) 取 #i (详情页标题)

  // skill 切面: bySkill (SkillCall 事件维度). 每个 skill 调用带 (callerAgentId, callerTurn) turn 锚点 (D6/D7).
  // D8/D9: turn chip = root 调 → 紫 "root·tN" 点 drillRoot(turn) 进 spawn 详情; subagent 调 → 蓝 "type#i·tN" 点 drillSpawn(agentId,turn) 进 spawn 详情 (Q3: 与 root 对称, 非旧 drillTurn 直进 turn 原文 — showTurn 不隐藏 session 视图 致点不动).
  // #i (spawn 序号) 并入 turn chip (不再单画 spawn #i); 删 sess 列 (恒=1 废列); callerTypes 聚合列保留. offline 未重建 SkillCall → 诚实标空.
  let skillHtml = "";
  const bySkill = d.bySkill || [];
  if (bySkill.length) {
    const rows = bySkill.map(sk => {
      const callers = (sk.callerTypes && Object.keys(sk.callerTypes).length)
        ? Object.entries(sk.callerTypes).sort((a, b) => b[1] - a[1]).map(([k, v]) => `${esc(k)}×${v}`).join(" ")
        : '<span class="faint">—</span>';
      // turn 锚点 chips: 每个非 None turn 一枚 (callerAgentId+callerTurn). root(无 agentId)→紫; subagent→蓝带 type#i.
      const turnChips = (sk.turns || [])
        .filter(t => t && t.turn != null)
        .map(t => {
          if (!t.agentId) {   // root 直调 (callerAgentId None)
            return `<span class="skill-turn root" data-agentid="root" data-turn="${t.turn}" title="root 主线 turn ${t.turn} · 点进看详情">root·t${t.turn}</span>`;
          }
          const tt = typeByAgent[t.agentId] || "?";
          const lbl = (idxByAgent[t.agentId] != null) ? `#${idxByAgent[t.agentId]}` : "";
          return `<span class="skill-turn" data-agentid="${esc(t.agentId)}" data-turn="${t.turn}" title="${esc(tt)}${lbl} · turn ${t.turn} · 点进看详情">${esc(tt)}${lbl}·t${t.turn}</span>`;
        }).join(" ");
      return `<div class="skill-row">`
        + `<span class="skill-name">${esc(sk.skillName || "?")}</span>`
        + `<span class="skill-num">${sk.calls || 0}</span>`
        + `<span class="skill-num">${(sk.spawns != null ? sk.spawns : (sk.spawnIds || []).length) || 0}</span>`
        + `<span class="skill-caller">${callers}${turnChips ? ` <span class="skill-turns">${turnChips}</span>` : ""}</span></div>`;
    }).join("");
    skillHtml = `<section class="card"><div class="sec-head"><h2>skills · <b>${bySkill.length}</b></h2>`
      + `<span class="note">该 session 用了哪些 skill (SkillCall 事件) · 列 = skill / calls / spawns / 调用方×次数 + turn 锚点 (紫 root·tN · 蓝 type#i·tN · 点进看详情)</span></div>`
      + `<div class="skill-head"><span>skill</span><span class="skill-num">calls</span><span class="skill-num">spawn</span><span>caller × 次数 + turn 锚点</span></div>`
      + `<div class="skill-list">${rows}</div></section>`;
  } else {
    skillHtml = `<section class="card"><div class="sec-head"><h2>skills</h2></div>`
      + `<p class="faint">该 session 无 SkillCall 事件记录 (offline transcript 未重建 Skill tool_use → SkillCall, 待重建). live record.py 的 SkillCall 事件可显.</p></section>`;
  }

  // 拓扑缩进树: callChains 建 parent→children 树. callerAgentId 空 = root 直调 (depth 2), 否则父 spawn (嵌套 depth-3+).
  // 每节点后挂锚点 ↗tN (caller→spawn 详情): callerAgentId 定 caller (root/父 spawn, 处理嵌套), callerTurn = 调用方启动该 spawn 的 turn;
  // 点锚点 → 调用方详情 定位, 点行 → 本 spawn 详情 (不变). 长树只显前 TOPO_SHOW 个 spawn, 其余折进"⋯ N 个 spawn"条, 点展开全显 (镜像 renderTurnList 折叠). 环保护防脏数据死循环.
  let topoHtml = "";
  if (spawns.length) {
    const byParent = {};
    spawns.forEach(c => { const k = c.callerAgentId || "ROOT"; (byParent[k] = byParent[k] || []).push(c); });
    const maxDepth = spawns.reduce((m, c) => Math.max(m, c.depth || 2), 2);
    const nodes = [];   // branch 收集 (带 depth/锚点数据), 渲染时再按扁平序号折叠
    function branch(parentId, depth, seen) {
      (byParent[parentId] || []).forEach(c => {
        const aid = c.spawnedAgentId || "";
        if (seen.has(aid)) return;          // 环保护 (observe don't crash)
        seen.add(aid);
        nodes.push({ aid, depth, sub: c.subagentType || "unknown", ti: idxByAgent[aid],
                     ct: c.callerTurn, callerAgentId: c.callerAgentId, parentType: c.parentType,
                     nodeDepth: c.depth || depth });
        branch(aid, depth + 1, seen);
      });
    }
    branch("ROOT", 1, new Set(["ROOT"]));
    function nodeHtml(nd, extra) {     // extra = "topo-folded" (折叠区节点) 或 "" (可见)
      // 锚点 (caller→spawn 详情): callerAgentId→root/父 spawn (嵌套); callerTurn = 调用方启动该 spawn 的 turn. != null 才显.
      const isRootCaller = !nd.callerAgentId;
      const anchor = (nd.ct != null && nd.ct !== "")
        ? ` <span class="topo-anchor${isRootCaller ? " root" : ""}" data-caller="${esc(isRootCaller ? "root" : nd.callerAgentId)}" data-turn="${esc(String(nd.ct))}" title="调用方 ${isRootCaller ? "主线 (root)" : esc(nd.parentType || "spawn")} turn ${nd.ct} · 点进 (调用方启动该 spawn 处)">↗t${nd.ct}</span>`
        : "";
      return `<div class="topo-node${extra ? " " + extra : ""}" style="margin-left:${nd.depth * 18}px" data-agentid="${esc(nd.aid)}"`
        + ` title="${esc(nd.sub)} · ${nd.ti != null ? "#" + nd.ti : "无#"} · depth ${nd.nodeDepth} · 点行看本 spawn 详情 / 点 ↗tN 看调用方详情">`
        + `<span class="topo-arm">└</span><span class="topo-type">${esc(nd.sub)}</span>${nd.ti != null ? ` <span class="row-idx">#${nd.ti}</span>` : ""}${anchor}</div>`;
    }
    const TOPO_SHOW = 8;    // 默认显前 8 个 spawn (root 另计); 超过 → 其余折进"⋯ N 个 spawn", 点展开全显 (镜像 renderTurnList FOLD)
    let tree = `<div class="topo-node root" data-agentid="root" title="orchestrator · 主线 · 点进 root 详情"><span class="topo-role">orchestrator</span> 主线 (root)</div>`;
    if (nodes.length <= TOPO_SHOW) {
      tree += nodes.map(n => nodeHtml(n)).join("");
    } else {
      tree += nodes.slice(0, TOPO_SHOW).map(n => nodeHtml(n)).join("");
      tree += `<div class="topo-fold" data-topofold data-rest="${nodes.length - TOPO_SHOW}" title="点展开剩余 ${nodes.length - TOPO_SHOW} 个 spawn">⋯ 还有 ${nodes.length - TOPO_SHOW} 个 spawn · 点展开全显</div>`;
      tree += nodes.slice(TOPO_SHOW).map(n => nodeHtml(n, "topo-folded")).join("");
    }
    const depthNote = maxDepth <= 2
      ? `<span class="note">仅 root→agent (depth-2) 可见 · agent→agent 嵌套 (depth-3+) 须 live hook · 点 spawn 后 ↗tN 看调用方详情</span>`
      : `<span class="note">最深 depth-${maxDepth} (含 agent→agent 嵌套, 来自 live 源) · 点 spawn 后 ↗tN 看调用方详情</span>`;
    topoHtml = `<section class="card"><div class="sec-head"><h2>调用拓扑 · call tree</h2>${depthNote}</div>`
      + `<div class="topo-tree">${tree}</div></section>`;
  }

  // §8.6 详情页健康状况下沉: 镜像 fleet 三信号 (💥ctx/⚠低命中/⏳异步) + ✗ tool 失败 (root + per-spawn 汇总).
  // 复用 fleet helper (isBlown/isLowHit, 零新计算) on perSession[0] (== 本 session 的 fleet 行, 带 ctxLimitErrors/asyncCount/toolErrorCount).
  // 全 0 显 ✓ 健康 (零噪声, 不吵健康 session); ✗ 段拆 root/spawn 指向元凶 (per-spawn ✗ 亦在下方花名册行内标). ctx 峰值已在 meta-chips, 此处只标异常.
  const _sessRow = (d.perSession || [])[0] || {};
  const _hBlown = isBlown(_sessRow);
  const _hLow = isLowHit(_sessRow);
  const _hAsync = _sessRow.asyncCount || 0;
  const _hRootTE = _sessRow.toolErrorCount || 0;
  const _hSpawnTE = segs.reduce((a, s) => a + (s.toolErr || 0), 0);
  const _hTE = _hRootTE + _hSpawnTE;
  const _hSegs = [];
  if (_hBlown) _hSegs.push(`<span title="ctx 压缩失败/逼爆 (context window limit API Error; 事件驱动, 非逼近 limit 也可能爆)">💥 <b>${((_sessRow.ctxLimitErrors)||{}).count||0}</b> ctx 爆掉</span>`);
  if (_hLow) _hSegs.push(`<span title="计费 cache 命中 <60% (省钱靶子: 几乎全全价 input)">⚠ 低命中 <b>${sessHitVal!=null?(sessHitVal*100).toFixed(0)+'%':'—'}</b></span>`);
  if (_hAsync) _hSegs.push(`<span title="async_launched 未回报的 spawn (飞行中/悬挂; 非 failed)">⏳ <b>${_hAsync}</b> 异步未回报</span>`);
  if (_hTE) {
    const _teWhere = (_hRootTE && _hSpawnTE) ? `(root ${_hRootTE} · spawns ${_hSpawnTE})`
      : _hRootTE ? `(root 主线)` : `(spawns)`;
    _hSegs.push(`<span title="tool_result is_error (Bash 非零退出/Edit 未命中等 provider 层失败; 与 spawn status 分轨, 不并 successRate). 点下方花名册 ✗ 行 → 逐 turn 定位">✗ <b>${_hTE}</b> tool 失败 ${_teWhere}</span>`);
  }
  const healthHtml = _hSegs.length
    ? `<div class="trust bad l2-health">${_hSegs.join(' · ')}</div>`
    : `<div class="trust ok l2-health">✓ 健康 · 0 异常</div>`;

  // 渲染
  document.getElementById("fleet-view").classList.add("hidden");
  const v = document.getElementById("level2-view");
  v.classList.remove("hidden");
  v.innerHTML =
    `<button class="back-btn">← 返回总览</button>` +
    `<div class="l2-head"><h1>session <span class="sid">${esc((sid || "").slice(0, 12))}</span></h1>` +
    `<div class="meta-chips">`
    + `<span class="chip">spawns <b>${spawns.length}</b></span>`
    + (asyncN ? `<span class="chip warn" title="run_in_background 后台 agent, 时间轴以竖线标启动时刻; 非 failed">异步 <b>${asyncN}</b></span>` : "")
    + (failN ? `<span class="chip err" title="status 既非 completed 也非 async_launched">失败 <b>${failN}</b></span>` : "")
    + `<span class="chip">total <b>${fmt(sessTot)}</b></span>`
    + `<span class="chip">cache hit <b>${sessHitVal != null ? (sessHitVal * 100).toFixed(1) + '%' : '—'}</b></span>`
    + `<span class="chip">ctx peak <b>${fmt(peak)}</b></span>`
    + `</div></div>`
    + healthHtml
    + `<section class="card"><div class="sec-head"><h2>时间轴 · wall-clock</h2>`
    + `<span class="note">轴 0→总时长 (已去 ms) · 段=subagent (长=duration, 色=type) · 上方紫点=root 主线 turn (离散, 琥珀环=ctx peak, 点进看原文) · 段间空档: "含 N root turn"=root 在活动 / "空闲 Nm"=真空 · 竖线=异步启动 (×N=同 turn 并发) · 点段/线看详情</span></div>`
    + `<div class="gantt-axis"><span>0</span><span>${fmtDurMs(span / 2)}</span><span>${fmtDurMs(span)}</span></div>`
    + `<div class="gantt">${ganttHtml || '<span class="faint">无 spawn 段 (零 depth-2 嵌套).</span>'}</div></section>`
    + `<div class="twin"><section class="card"><div class="sec-head"><h2>cache 半边 · per-spawn 命中</h2>`
    + `<span class="note">命中率 高→低 · 点行看详情 · …N → 下方 agents 面板看全部</span></div>`
    + `<div class="twin-list dist">${cacheBars}</div></section>`
    + `<section class="card"><div class="sec-head"><h2>context 半边 · root 主线逐 turn</h2>`
    + `<span class="note">grow→peak→drop · root per-turn 可算, per-spawn 峰值算不出</span></div>`
    + `${svgBlock}</section></div>`
    + `${agentsHtml}`
    + `${skillHtml}`
    + `${topoHtml}`
    + `${olHtml}`;
  v.querySelector(".back-btn").addEventListener("click", backToFleet);
  // session 视图→spawn 详情 钻取委托: gantt (段 + async 竖线) / cache 书挡行 / agents 行 / 拓扑节点 点 → drillSpawn (§8.6)
  // 并发簇 (×N 同刻): 不直接进 spawn 详情 (只首个, 其余 N-1 不可达), 先滚到 agents 面板 + 锁定高亮全量成员, 用户挑一行再进 (用户提议).
  v.querySelector(".gantt").addEventListener("click", (e) => {
    // DR5: root 主线离散紫点 → drillRoot 进 root 详情 (定位到该 turn; 在 seg/async 之前判)
    const rd = e.target.closest(".root-dot");
    if (rd && rd.dataset.turn != null && rd.dataset.turn !== "") { drillRoot(rd.dataset.turn); return; }
    const el = e.target.closest(".gantt-seg, .gantt-async");   // closest: 点 ×N 胶囊子元素也归位到竖线
    if (!el) return;
    if (el.classList.contains("multi")) {                       // 并发簇: 滚到 agents 面板 + 高亮全量成员 (区别 hover .hl, 用限时 .sel)
      const ids = (el.dataset.agentids || "").split(",");
      v.querySelectorAll(".agent-row.sel").forEach(r => r.classList.remove("sel"));   // 清上一次锁定
      const pan = document.getElementById("agents-panel");
      if (pan) pan.scrollIntoView({behavior:"smooth", block:"start"});
      v.querySelectorAll(".agent-row").forEach(r => {
        if (ids.includes(r.dataset.agentid)) {
          r.classList.add("sel");
          r.classList.add("flash");
          setTimeout(() => r.classList.remove("flash"), 1300);  // 短闪两次吸引注意
          setTimeout(() => r.classList.remove("sel"), 2600);    // 锁定框限时 ~2.6s 自动淡出 (非持久; 进 spawn 详情 / 点别的 也会清, 见 drillSpawn)
        }
      });
      return;
    }
    const aid = el.dataset.agentid;                             // 单段/单竖线: 无歧义, 直接进 spawn 详情
    if (aid) drillSpawn(aid);
  });
  v.querySelector(".twin-list").addEventListener("click", (e) => {
    const row = e.target.closest(".dist-row");
    if (!row) return;
    if (row.dataset.agentid) drillSpawn(row.dataset.agentid);
    else if (row.classList.contains("dist-more")) jumpToAgentsPanel();
  });
  // DR5: context sparkline 逐点 (透明 .spark-pt 命中圆) → drillRoot 进 root 详情 (定位到该 turn)
  const sparkEl = v.querySelector(".spark");
  if (sparkEl) sparkEl.addEventListener("click", (e) => {
    const pt = e.target.closest(".spark-pt");
    if (pt && pt.dataset.turn != null) drillRoot(pt.dataset.turn);
  });
  const agentListEl = v.querySelector(".agent-list");   // guard: 无 spawn 时 agents 面板不存在
  if (agentListEl) agentListEl.addEventListener("click", (e) => {
    const row = e.target.closest(".agent-row");
    const aid = row && row.dataset.agentid;
    if (aid) drillSpawn(aid);
  });
  const topoTreeEl = v.querySelector(".topo-tree");     // guard: 无 spawn 时拓扑不存在
  if (topoTreeEl) {
    const topoFold = topoTreeEl.querySelector("[data-topofold]");   // 长树折叠条: 点 toggle 展开/收起 (.topo-expanded 控 .topo-folded 显隐), 文案 + title 随状态切
    if (topoFold) topoFold.addEventListener("click", () => {
      const rest = topoFold.dataset.rest;
      const expanded = topoTreeEl.classList.toggle("topo-expanded");
      topoFold.textContent = expanded ? `▴ 收起 (隐 ${rest} 个 spawn)` : `⋯ 还有 ${rest} 个 spawn · 点展开全显`;
      topoFold.title = expanded ? `点收起, 隐剩余 ${rest} 个 spawn` : `点展开剩余 ${rest} 个 spawn`;
    });
    topoTreeEl.addEventListener("click", (e) => {
      const anchor = e.target.closest(".topo-anchor");      // 锚点 (caller→spawn 详情): depth-2 → drillRoot(callerTurn); depth-3 → drillSpawn(父 spawn, callerTurn). anchor 是节点子元素, closest 命中即 return (不与点行冲突)
      if (anchor && anchor.dataset.turn != null && anchor.dataset.turn !== "") {
        if (anchor.dataset.caller === "root") drillRoot(anchor.dataset.turn);
        else drillSpawn(anchor.dataset.caller, anchor.dataset.turn);
        return;
      }
      const node = e.target.closest(".topo-node[data-agentid]");
      const aid = node && node.dataset.agentid;
      if (aid === "root") { drillRoot(); return; }   // D6: 拓扑根 → 进 root 详情 (无 spawn id "root", 单独分支; drillRoot 无参=不定位)
      if (aid) drillSpawn(aid);                       // spawn 行本身 → 本 spawn 自己的详情 (行为不变)
    });
  }
  const skillListEl = v.querySelector(".skill-list");   // skill 切面 spawn #i chip → drillSpawn (root 调用无 chip)
  if (skillListEl) skillListEl.addEventListener("click", (e) => {
    // D9/DR5/Q3: turn 锚点 chip — root → drillRoot 进 root 详情 (定位 turn); subagent → drillSpawn 进 spawn 详情 (定位 callerTurn, 与 root 对称); 旧 .skill-ref → spawn 详情 (兼容).
    // 注: 不直进 turn 原文 (drillTurn) — showTurn 不隐藏 session 视图, 直跳 turn 原文 会留 session 视图 skill 面板可见 → 视觉无变化像"点不动"; 走 drillSpawn 进 spawn 详情 才正确切层 (showSpawn 隐藏 session 视图).
    const turn = e.target.closest(".skill-turn");
    if (turn && turn.dataset.agentid != null && turn.dataset.turn != null) {
      if (turn.dataset.agentid === "root") drillRoot(turn.dataset.turn);
      else drillSpawn(turn.dataset.agentid, turn.dataset.turn);
      return;
    }
    const ref = e.target.closest(".skill-ref");
    if (ref && ref.dataset.agentid) drillSpawn(ref.dataset.agentid);
  });
  // timeline ↔ agents 双向悬停高亮 (同名并发消歧): 悬停时间轴段/竖线 → 高亮对应 agent-row(s);
  // 悬停 agents 行 → 高亮对应时间轴元素. 并发簇 data-agentids 多 id 全高亮, 直接 "看到" 哪几行而非读序号.
  v.querySelector(".gantt").addEventListener("mouseover", (e) => {
    const el = e.target.closest(".gantt-seg, .gantt-async");
    const ids = el && el.dataset.agentids ? el.dataset.agentids.split(",") : null;
    v.querySelectorAll(".agent-row").forEach(r =>
      r.classList.toggle("hl", !!(ids && ids.includes(r.dataset.agentid))));
  });
  v.querySelector(".gantt").addEventListener("mouseleave", () =>
    v.querySelectorAll(".agent-row.hl").forEach(r => r.classList.remove("hl")));
  if (agentListEl) {
    agentListEl.addEventListener("mouseover", (e) => {
      const row = e.target.closest(".agent-row");
      const id = row && row.dataset.agentid;
      v.querySelectorAll(".gantt-seg, .gantt-async").forEach(el => {
        const ids = (el.dataset.agentids || "").split(",");
        el.classList.toggle("hl", !!(id && ids.includes(id)));
      });
    });
    agentListEl.addEventListener("mouseleave", () =>
      v.querySelectorAll(".gantt-seg.hl, .gantt-async.hl").forEach(el => el.classList.remove("hl")));
  }
}

function backToFleet() {
  document.getElementById("level2-view").classList.add("hidden");
  document.getElementById("fleet-view").classList.remove("hidden");
}

// === spawn view (§8.6: spawn 头 + 逐 turn traces + outlier 归因 + 折叠) ===
let _sessionCtx = null;   // {sid, sessionHit} — showSession 设, showSpawn 读 (hit 对比)
let _spawnAgentId = null; // showSpawn 设, drillTurn 读 (turn 原文 钻取键)
let _spawnTurns = null;   // 折叠展开重渲用
let _turnOrigin = null;   // D11/DR6: drillTurn 设 ("spawn"=subagent / "root"=主线); showTurn 按 origin 切 head 文案/label (turn 原文 back 统一 backToSpawn 回 spawn 详情)

function drillSpawn(agentId, focusTurn, onDone) {
  if (!agentId || !_sessionCtx) return;
  document.querySelectorAll(".agent-row.sel").forEach(r => r.classList.remove("sel"));  // 进 spawn 详情 即清并发簇锁定, 避免返回时残留高亮
  fetch("/api/spawn/" + encodeURIComponent(_sessionCtx.sid) + "/" + encodeURIComponent(agentId))
    .then(r => r.json()).then(d => {
      if (d.error) { alert("spawn load failed: " + d.error); return; }
      showSpawn(d, agentId, focusTurn);
      if (onDone) onDone();   // 链式钻取 (skill 展开 turn → spawn 详情 → turn 原文): showSpawn 渲染完 level3 再进 level4, 保返回栈
    }).catch(e => alert("spawn fetch error: " + e));
}

function drillRoot(turnIndex, onDone) {
  // DR3: root 主线 → root 详情 (镜像 drillSpawn). fetch /api/root/<sid> → showRoot(d, focusTurn=turnIndex).
  // root = 主线 transcript 本身 (无 agentId), 故 URL 只带 sid; turnIndex 仅作入场定位锚 (定位到该 turn 行).
  if (!_sessionCtx) return;
  document.querySelectorAll(".agent-row.sel").forEach(r => r.classList.remove("sel"));  // 进 spawn 详情 即清并发簇锁定
  fetch("/api/root/" + encodeURIComponent(_sessionCtx.sid))
    .then(r => r.json()).then(d => {
      if (d.error) { alert("root load failed: " + d.error); return; }
      showRoot(d, turnIndex);
      if (onDone) onDone();   // 链式钻取 (skill 展开 root turn → root 详情 → turn 原文): showRoot 渲染完 level3 再进 level4, 保返回栈
    }).catch(e => alert("root fetch error: " + e));
}

function renderTurnRow(t, i) {
  // per-turn token 混合显示 (§8.6 边界2): usage 非零 → 真 token; 记 0 → result 字符数代理.
  // ctx = 本 turn 完整输入侧上下文 (input+cacheCreation+cacheRead, 三者不重叠); cacheRead=重读已缓存, 随会话累加主导增长.
  // (旧式 input+cacheRead 漏 cacheCreation → turn0 仅显 6, 因首 turn cr=0 全进 cc; 已修). ⚠ outlier 另按 burden 算 (见 note).
  const ctxTokens = (t.input||0)+(t.cacheCreation||0)+(t.cacheRead||0);
  // 明细 in/cc/cr (in=未缓存零头, cc=新写缓存, cr=重读已缓存/随会话累加) — 淡于 ctx 本体, 便于看清 ctx 由哪段撑起.
  const brk = ` <span class="tok-break" title="in=本 turn 未缓存零头 · cc=新写进缓存 · cr=重读已缓存(随会话累加)">in ${fmt(t.input||0)} · cc ${fmt(t.cacheCreation||0)} · cr ${fmt(t.cacheRead||0)}</span>`;
  const burden = t.usageIsReal
    ? `<span class="tok-real">ctx ${fmt(ctxTokens)}${brk} · out ${fmt(t.output)}</span>`
    : `<span class="tok-proxy" title="该 turn usage 记 0 (provider artifact); 回退 result 字符数代理 (字符≠token, 只作相对大小)">≈${fmt(t.resultChars||0)} chars*</span>`;
  const tools = (Array.isArray(t.tools) && t.tools.length)
    ? t.tools
    : (t.tool ? [{name: t.tool, target: t.target}] : []);   // 回退: 旧契约 tool/target (单 tool) 或 text turn
  // 每 tool_use 一行: .turn-tools 是 grid (max-content 列 → 所有 tool 名按最宽自动对齐), 名字 + 各自 target
  const tool = tools.length
    ? `<span class="turn-tools">${tools.map(tl =>
        `<span class="turn-tool">${esc(tl.name || "")}</span>${tl.target
          ? `<span class="turn-target">${esc(tl.target)}</span>`
          : `<span class="turn-target faint">—</span>`}`
      ).join("")}</span>`
    : `<span class="faint">(text)</span>`;
  // §8.6 ✗ tool 失败: 本 turn 内 is_error 的 tool_use → tool 名 (agent_turn_traces 已采 toolErrors[]). col5 占位 (健康 turn 空 cell 塌缩, burden 恒 col6 右对齐).
  const errs = (Array.isArray(t.toolErrors) && t.toolErrors.length)
    ? `<span class="turn-err" title="本 turn 失败 tool (tool_result is_error): ${esc(t.toolErrors.join(', '))}">✗ ${esc(t.toolErrors.join(' · '))}</span>`
    : `<span></span>`;
  const ol = t.outlier ? " turn-outlier" : "";
  return `<div class="turn-row${ol}" data-turn="${i}" title="点进看原文">`
    + `<span class="turn-i">#${i}</span>`
    + `<span class="turn-ts">${esc((t.ts||"").slice(11,19))}</span>`
    + `<span class="turn-role">asst</span>${tool}${errs}`
    + `<span class="turn-burden">${burden}</span></div>`;
}

function renderTurnList(turns, forceAll, focusTurn) {
  // 折叠 (§8.6): n>20 → 头5+尾5+中间折叠; outlier(⚠) 中最重的 top-K 始终外露 (点 ⋯ forceAll 全展).
  // top-K 外露: 重尾 session (长 root 主线) outlier 可达数百个, 全外露会撑爆折叠; 至多外露 K 个最重的,
  // 既保折叠健康又把"最重的 turn"推到眼前. 均匀 session 无 outlier → 不外露 (守诚实). badge 仍按 t.outlier.
  // DR4: focusTurn (root 详情 入场定位) 行不被折叠 → 始终渲染, 供 showRoot scrollIntoView + 短闪 (undefined 时无副作用).
  const FOLD_N = 20, HEAD = 5, TAIL = 5, EXPOSE_K = 10;
  if (forceAll || turns.length <= FOLD_N) {
    return turns.map(renderTurnRow).join("");
  }
  // focusTurn 来自 dataset (字符串); 与 array index i (number) 须同型比较, 否则焦点行判定恒 false →
  // 被折叠吃掉 → showRoot 的 querySelector 找不到该行 → 入场不定位 (紫点/sparkline 点进 root 详情 不滚不闪).
  const ftNum = (focusTurn != null && focusTurn !== "") ? Number(focusTurn) : NaN;
  const expose = new Set(turns.map((t,i) => ({i, b: (t.burden||0)}))
    .filter(x => turns[x.i].outlier)          // 仅 outlier 入选 (均匀 session → 空集)
    .sort((a,b) => b.b - a.b)                  // 按 burden 降序
    .slice(0, EXPOSE_K).map(x => x.i));        // 取 top-K
  const keep = (i) => i < HEAD || i >= turns.length - TAIL || expose.has(i) || i === ftNum;
  let html = "", folded = 0;
  for (let i = 0; i < turns.length; i++) {
    if (keep(i)) {
      if (folded > 0) { html += `<div class="turn-fold">⋯ ${folded} turns 折叠 (点展开全显)</div>`; folded = 0; }
      html += renderTurnRow(turns[i], i);
    } else { folded++; }
  }
  if (folded > 0) html += `<div class="turn-fold">⋯ ${folded} turns 折叠 (点展开全显)</div>`;
  return html;
}

function showSpawn(d, agentId, focusTurn) {
  const head = d.head || {};
  const tk = head.tokens || {};
  const turns = (d.traces && d.traces.turns) || [];
  _spawnTurns = turns; _spawnAgentId = agentId;
  const spawnHit = head.hit;
  const idx = (_sessionCtx && _sessionCtx.idxByAgent && _sessionCtx.idxByAgent[agentId]);  // 时序 #i (对回 agents 面板行; showSession 挂进 ctx)
  const sessHitPct = _sessionCtx && _sessionCtx.sessionHit != null
    ? (_sessionCtx.sessionHit * 100).toFixed(1) : "—";

  // spawn 概要: agentType / dur / tokens / toolStats / prompt 摘要 (§8.6)
  const ts = head.toolStats || {};
  const statChips = [["Read", ts.readCount], ["Search", ts.searchCount], ["Bash", ts.bashCount],
    ["Edit", ts.editFileCount], ["+lines", ts.linesAdded], ["−lines", ts.linesRemoved]]
    .filter(x => x[1] != null).map(x => `<span class="chip">${x[0]} <b>${x[1]}</b></span>`).join("");
  // §8.6 ✗ head 徽章: 该 spawn 内部 tool_result is_error 失败计数 (provider/CC tool 执行失败, ≠ SubagentCall status; 见下方逐 turn ✗ 定位). status:completed 仍可有 toolErr — 两轨独立, 不并 success/fail.
  const spawnTE = head.toolErrorCount || 0;
  const teBadge = spawnTE > 0
    ? ` <span class="head-badge" title="该 spawn 内部 tool_result is_error 失败 ${spawnTE} 次 (provider/CC tool 执行失败, ≠ SubagentCall status; 见下方逐 turn ✗ 定位元凶)">✗ ${spawnTE} tool 失败</span>` : "";
  const headHtml =
    `<div class="l3-head"><h1>spawn ${idx != null ? `<span class="spawn-idx">#${idx}</span> ` : ""}`
    + `<span class="type-tag" style="color:${typeColor(head.agentType)}">${esc(head.agentType||"?")}</span>`
    + ` <span class="sid" title="agentId (唯一稳定锚点; 跨刷新不变, 供精确引用)">${esc((agentId||"").slice(0,12))}</span>${teBadge}</h1></div>`
    + `<section class="card"><div class="sec-head"><h2>spawn 概要</h2>`
    + `<span class="note">字段来自 root toolUseResult (真实非估算)</span></div>`
    + `<div class="spawn-grid">`
    + `<div><span class="m-lab">dur</span><b>${fmtDur((head.totalDurationMs||0)/1000)}</b></div>`
    + `<div><span class="m-lab">total</span><b>${fmt(tk.total)}</b></div>`
    + `<div><span class="m-lab">cacheRead</span><b>${fmt(tk.cacheRead)}</b></div>`
    + `<div><span class="m-lab">input</span><b>${fmt((tk.input||0)+(tk.cacheCreation||0))}</b></div>`
    + `<div><span class="m-lab">output</span><b>${fmt(tk.output)}</b></div>`
    + `<div><span class="m-lab">hit</span><b class="${spawnHit!=null&&spawnHit<60?'lo':spawnHit!=null&&spawnHit<85?'mid':'ok'}">${spawnHit!=null?spawnHit+'%':'—'}</b> <span class="faint">vs subagent ${sessHitPct}%</span></div>`
    + `</div>`
    + (statChips ? `<div class="stat-chips">${statChips}</div>` : "")
    + (head.promptSummary ? `<div class="prompt-sum" title="task prompt 摘要 (首100字符)">task: ${esc(head.promptSummary)} <span class="faint">(${fmt(head.promptChars)} chars)</span></div>` : "")
    + `</section>`;

  // outlier 归因 callout (§8.6 L552): hit 异常落可读结论
  let callout = "";
  if (spawnHit != null && _sessionCtx && _sessionCtx.sessionHit != null) {
    const av = _sessionCtx.sessionHit * 100;
    if (spawnHit < av - 15) {
      callout = `<div class="callout"><b>低命中归因</b> · spawn hit ${spawnHit}% vs subagent 均值 ${av.toFixed(1)}% · `
        + `疑 task 内联大块 / spawn 自读大文件 · 点最重的 turn (⚠) → 看它 Read 了啥 <span class="faint">(⚠ = 本 turn 新进上下文 input+cc &gt;1.5×均值; 不含 cacheRead 重读 — 故"最重"≠ctx 最大, ctx 随 cacheRead 累加靠后都大)</span></div>`;
    } else {
      callout = `<div class="callout ok-callout">spawn hit ${spawnHit}% ≈ subagent 均值 ${av.toFixed(1)}% · 命中正常</div>`;
    }
  }
  if (d.depth2Note) {
    callout += `<div class="callout muted-callout">边界: ${esc(d.depth2Note)}</div>`;
  }

  document.getElementById("fleet-view").classList.add("hidden");
  document.getElementById("level2-view").classList.add("hidden");
  const v = document.getElementById("level3-view");
  v.classList.remove("hidden");
  v.innerHTML = `<button class="back-btn">← 返回 session</button>` + headHtml + callout
    + `<section class="card"><div class="sec-head"><h2>逐 turn traces · ${turns.length} turns</h2>`
    + `<span class="note">usage 非零→真 token; 记 0→result 字符数代理* · ⚠ = 本 turn 新进上下文 (input+cc) &gt;1.5×均值 · 点行→看原文${focusTurn!=null?` · 当前定位 turn #${focusTurn}`:""}</span></div>`
    + `<div class="turn-list">${renderTurnList(turns, false, focusTurn) || '<span class="faint">无 turn (空 spawn / 平台边界).</span>'}</div></section>`;
  v.querySelector(".back-btn").addEventListener("click", backToSession);
  // DR4: 入场定位 focus turn (skill chip 带来的 callerTurn; 短闪 + 居中; 镜像 showRoot)
  if (focusTurn != null) {
    const row = v.querySelector(`.turn-row[data-turn="${focusTurn}"]`);
    if (row) { row.classList.add("flash"); row.scrollIntoView({block:"center"}); }
  }
  // turn 行 + 折叠 委托
  v.querySelector(".turn-list").addEventListener("click", (e) => {
    const fold = e.target.closest(".turn-fold");
    if (fold) { fold.parentElement.innerHTML = renderTurnList(_spawnTurns, true); return; }
    const row = e.target.closest(".turn-row");
    if (row && row.dataset.turn != null) drillTurn(_spawnAgentId, row.dataset.turn);
  });
}

function showRoot(d, focusTurn) {
  // DR1/DR3: root 主线 spawn 详情 (镜像 showSpawn). root = orchestrator, 无 spawn 头 (agentType/dur/toolStats/prompt),
  // 故头用主线聚合 (turnCount / peak ctx / 总 output / dur); peak/sum 服务端取自 root_context_samples
  // (与时间轴紫点 + sparkline 同源同口径). turn-list 复用 renderTurnRow + focusTurn 入场定位.
  const head = d.head || {};
  const turns = (d.traces && d.traces.turns) || [];
  _spawnTurns = turns; _spawnAgentId = "root";   // 复用 turn-list 委托 → drillTurn("root", i) 进 turn 原文
  const turnCount = head.turnCount != null ? head.turnCount : turns.length;
  const peak = head.peak;
  const outSum = turns.reduce((a, t) => a + (t.output||0), 0);
  const firstTs = turns.length ? turns[0].ts : null;
  const lastTs = turns.length ? turns[turns.length-1].ts : null;
  const dur = (firstTs && lastTs && !isNaN(Date.parse(firstTs)) && !isNaN(Date.parse(lastTs)))
    ? fmtDur((Date.parse(lastTs) - Date.parse(firstTs))/1000) : "—";

  // §8.3/§8.6 root 健康徽章: 💥 ctx 爆掉 (head.ctxLimitErrors, server _handle_root 已附) + ✗ tool 失败 (head.toolErrorCount). root = 主线, 爆掉/失败都在自己 transcript.
  const rootBlown = !!(head.ctxLimitErrors && head.ctxLimitErrors.count > 0);
  const rootTE = head.toolErrorCount || 0;
  const rootH = [];
  if (rootBlown) rootH.push(`<span class="head-badge" title="root 主线 context window limit API Error ${head.ctxLimitErrors.count}× (压缩失败/逼爆, §8.3; sample: ${String(head.ctxLimitErrors.sample||"").slice(0,70)})">💥 ${head.ctxLimitErrors.count} ctx 爆掉</span>`);
  if (rootTE > 0) rootH.push(`<span class="head-badge" title="root 主线 tool_result is_error 失败 ${rootTE} 次 (provider/CC tool 执行失败, ≠ SubagentCall status; 见下方逐 turn ✗ 定位元凶)">✗ ${rootTE} tool 失败</span>`);
  const rootBadges = rootH.length ? " " + rootH.join(" ") : "";
  const headHtml =
    `<div class="l3-head"><h1>root 主线 <span class="type-tag" style="color:var(--purple)">orchestrator</span>${rootBadges}</h1></div>`
    + `<section class="card"><div class="sec-head"><h2>root 概要</h2></div>`
    + `<div class="spawn-grid">`
    + `<div><span class="m-lab">turns</span><b>${turnCount}</b></div>`
    + `<div><span class="m-lab">peak ctx</span><b>${peak!=null?fmt(peak):'—'}</b>${rootBlown?` <span class="tool-err" title="峰值期 context window limit 已爆 (§8.3)">💥爆</span>`:""}</div>`
    + `<div><span class="m-lab">总 output</span><b>${fmt(outSum)}</b></div>`
    + `<div><span class="m-lab">dur</span><b>${dur}</b></div>`
    + `</div></section>`;

  // callout: 峰值 ctx 提示 (点 ⚠ turn / 峰值紫点看原文 → turn 原文) + depth2 边界; root 爆掉时改红 callout 强提示 (§8.3)
  let callout = peak != null
    ? `<div class="callout${rootBlown?' bad-callout':''}"><b>${rootBlown?'💥 已爆 ctx ':'峰值 ctx '}${fmt(peak)}</b> · 点最重的 turn (⚠) 或峰值紫点看原文 <span class="faint">(⚠ = 本 turn 新进上下文 input+cc &gt;1.5×均值; 不含 cacheRead 重读 — 故"最重"≠ctx 最大, ctx 随 cacheRead 累加靠后都大)</span></div>` : "";
  if (d.depth2Note) callout += `<div class="callout muted-callout">边界: ${esc(d.depth2Note)}</div>`;

  document.getElementById("fleet-view").classList.add("hidden");
  document.getElementById("level2-view").classList.add("hidden");
  const v = document.getElementById("level3-view");
  v.classList.remove("hidden");
  v.innerHTML = `<button class="back-btn">← 返回 session</button>` + headHtml + callout
    + `<section class="card"><div class="sec-head"><h2>逐 turn traces · ${turns.length} turns</h2>`
    + `<span class="note">点行→看原文${focusTurn!=null?` · 当前定位 turn #${focusTurn}`:""} · ⚠ = 本 turn 新进上下文 (input+cc) &gt;1.5×均值</span></div>`
    + `<div class="turn-list">${renderTurnList(turns, false, focusTurn) || '<span class="faint">无 turn.</span>'}</div></section>`;
  v.querySelector(".back-btn").addEventListener("click", backToSession);

  // DR4: 入场定位 focus turn (短闪 + 居中); forceAll 展开后不重闪 (仅入场一次).
  if (focusTurn != null) {
    const row = v.querySelector(`.turn-row[data-turn="${focusTurn}"]`);
    if (row) { row.classList.add("flash"); row.scrollIntoView({block:"center"}); }
  }

  // turn 行 + 折叠 委托 (与 spawn 同款; _spawnAgentId="root" → drillTurn("root", i) 进 turn 原文)
  v.querySelector(".turn-list").addEventListener("click", (e) => {
    const fold = e.target.closest(".turn-fold");
    if (fold) { fold.parentElement.innerHTML = renderTurnList(_spawnTurns, true, focusTurn); return; }
    const row = e.target.closest(".turn-row");
    if (row && row.dataset.turn != null) drillTurn(_spawnAgentId, row.dataset.turn);
  });
}

function backToSession() {
  document.getElementById("level3-view").classList.add("hidden");
  document.getElementById("level2-view").classList.remove("hidden");
}

// === turn logs (§8.6: 一个 turn 的原文 · F9 on-demand raw · 本地原始内容) ===
// D4: 显式接 agentId (root sentinel="root"); 不再读全局 _spawnAgentId → root turn 与 subagent turn 同入口进 turn 原文.
// D11: 记 _turnOrigin, showTurn back-btn 按 origin 切 "← 返回 spawn"(spawn 详情)/"← 返回 session"(session 视图) 与回调.
function drillTurn(agentId, turnIndex) {
  if (!_sessionCtx || agentId == null) return;
  _turnOrigin = (agentId === "root") ? "root" : "spawn";
  fetch("/api/turn/" + encodeURIComponent(_sessionCtx.sid) + "/" + encodeURIComponent(agentId)
        + "/" + encodeURIComponent(turnIndex))
    .then(r => r.json()).then(d => {
      if (d.error) { alert("turn load failed: " + d.error); return; }
      showTurn(d, turnIndex);
    }).catch(e => alert("turn fetch error: " + e));
}

function _fmtJson(v) {
  // tool_use.input → 折行 JSON 字符串 (turn 原文 raw 全文, 跨 F9 deliberately)
  try { return esc(JSON.stringify(v, null, 2)); } catch (e) { return esc(String(v)); }
}
function _fmtResult(c) {
  // tool_result.content (str 或 [{text}]) → 文本 (turn 原文 raw)
  if (typeof c === "string") return esc(c);
  if (Array.isArray(c)) return c.map(b => esc((b && (b.text || b.content)) || JSON.stringify(b))).join("\n");
  return esc(JSON.stringify(c, null, 2));
}

function showTurn(d, turnIndex) {
  const blocks = d.blocks || [];
  const results = d.results || [];
  // 折叠策略 (§8.6 L573): tool_use.input 折到 ~8 行; tool_result 折到 ~10 行; 点展开
  let bodyHtml = "";
  for (const b of blocks) {
    if (b.type === "text") {
      bodyHtml += `<div class="raw-block raw-text foldable"><div class="raw-kind">assistant text</div>`
        + `<pre>${esc(b.text || "")}</pre></div>`;
    } else if (b.type === "tool_use") {
      bodyHtml += `<div class="raw-block raw-tooluse foldable"><div class="raw-kind">tool_use · ${esc(b.name||"?")}</div>`
        + `<pre>${_fmtJson(b.input)}</pre></div>`;
    }
  }
  for (const r of results) {
    bodyHtml += `<div class="raw-block raw-result foldable${r.isError?' is-error':''}">`
      + `<div class="raw-kind">tool_result${r.isError?' · ERROR':''} · ${esc((r.toolUseId||"").slice(0,8))}</div>`
      + `<pre>${_fmtResult(r.content)}</pre></div>`;
  }
  const usage = d.usage ? `<span class="chip">usage ${esc(JSON.stringify(d.usage))}</span>` : "";

  document.getElementById("level3-view").classList.add("hidden");
  const v = document.getElementById("level4-view");
  v.classList.remove("hidden");
  // DR6: root turn 现经 root 详情 (不再 session 视图→turn 原文 直跳), 故 turn 原文 back 统一回 spawn 详情 (backToSpawn); _turnOrigin 仅切 head 文案/label.
  const isRoot = _turnOrigin === "root";
  const backLabel = isRoot ? "← 返回 root 主线" : "← 返回 spawn";
  const rawSrc = isRoot ? "主线 session transcript" : "agent-*.jsonl";
  v.innerHTML = `<button class="back-btn">${backLabel}</button>`
    + `<div class="raw-tag">turn 原文 · 直接读自 ${rawSrc}（逐字未改的原始记录 · 不依赖 token 聚合）</div>`
    + `<div class="l4-head"><h1>${isRoot ? "root " : ""}turn #${turnIndex} <span class="faint">· ${esc(d.stop_reason||"?")}</span></h1>`
    + `<div class="meta-chips">${usage}</div></div>`
    + (bodyHtml || '<span class="faint">空 turn.</span>');
  v.querySelector(".back-btn").addEventListener("click", backToSpawn);
  // 折叠展开 (§8.6: 默认折叠, 点开全展)
  v.querySelectorAll(".foldable").forEach(el =>
    el.addEventListener("click", () => el.classList.toggle("expanded")));
}

function backToSpawn() {
  document.getElementById("level4-view").classList.add("hidden");
  document.getElementById("level3-view").classList.remove("hidden");
}

// === live-tail (§8.8): 前端 2s 轮询重渲染 fleet pane; 点进详情 / 切到别的浏览器标签页时暂停 ===
// server /api/result 已 mtime-poll 自动 refresh (E3); 前端只管周期重拉 + 重渲染 fleet.
// 顶部 mode chip 三态 (刷新轴 × 数据活性): live-tail 关 → ⏸暂停; 开 且 源在动 → ●实时; 开 但 源长期静止 → ⏳静止 (旧 session 不再活动).
// 跟数据源 (来源轴, 下方"数据源"下拉) 是两回事.
function modeChipState() {
  if (!_liveTailOn) return { cls: "paused", text: "⏸ 暂停" };
  return _lastDataActive ? { cls: "live", text: "● 实时" } : { cls: "stale", text: "⏳ 静止" };
}
function updateLiveChip() {   // 开关瞬间即时更新 DOM; render() 每次也按模式态重建, 此处补切换瞬间 (含 静止↔实时 切换)
  const c = document.getElementById("mode-chip");
  if (!c) return;
  const ms = modeChipState();
  c.className = "chip " + ms.cls;
  c.textContent = ms.text;
}
function initLiveTail() {
  const btn = document.getElementById("live-toggle");
  const POLL_MS = 2000;
  let timer = null;
  const pollOnce = () => {
    if (document.hidden) return;                       // 切到别的浏览器标签页 → 暂停 (visibilitychange 恢复)
    const fv = document.getElementById("fleet-view");
    if (!fv || fv.classList.contains("hidden")) return; // 点进详情 (session/spawn/turn) → 跳过, 不覆盖详情视图
    fetch("/api/result").then(r => r.json()).then(d => render(d)).catch(() => {}); // 静默失败, 下轮重试
  };
  const start = () => {
    if (timer) return;
    timer = setInterval(pollOnce, POLL_MS);
    if (btn) { btn.classList.add("on"); btn.setAttribute("aria-pressed", "true"); }
    _liveTailOn = true; updateLiveChip();
  };
  const stop = () => {
    if (!timer) return;
    clearInterval(timer); timer = null;
    if (btn) { btn.classList.remove("on"); btn.setAttribute("aria-pressed", "false"); }
    _liveTailOn = false; updateLiveChip();
  };
  if (btn) btn.addEventListener("click", () => { timer ? stop() : start(); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) pollOnce(); }); // 回前台立即补刷一次
  start();   // 默认开
}

// === 主题切换 (深/浅) · 2026-06-23 · <head> inline 脚本首帧前已按 localStorage / 系统偏好设好 data-theme (防 FOUC);
//     这里只接按钮: 点 → 切 data-theme + 记 localStorage['ai-theme'] + 同步图标. 图标显目标态 (深→☀️ 浅→🌙).
function initTheme() {
  const btn = document.getElementById("theme-toggle");
  if (!btn) return;
  const ICON = { dark: "☀️", light: "🌙" };   // 显目标态: 当前深显☀️(点切浅), 当前浅显🌙(点切深)
  const sync = () => {
    const cur = document.documentElement.getAttribute("data-theme") || "dark";
    btn.textContent = ICON[cur] || ICON.dark;
  };
  sync();
  btn.addEventListener("click", () => {
    const cur = document.documentElement.getAttribute("data-theme") || "dark";
    const next = cur === "light" ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem("ai-theme", next); } catch (e) {}  // 隐私模式禁 localStorage → 静默, 本 session 仍切
    sync();
  });
}

// === 运行时数据源切换器 (POST /api/source · §8 dashboard): 不重启 server 切数据源 ===
// 类型判断在 server (_infer_source: 裸 path → scan/live/transcript); 前端只管 POST + 成功回 fleet-view 重拉.
// 下拉 = 常用 path 收藏 (GET /api/presets, 友好名); 路径框 = 粘贴裸 path; 浏览弹层 = 选目录/文件 (裸 path).
// syncFromCurrent: 从 server 当前 SOURCE 回填 select/input (启动 + 切源后 + 失败回退时). cur 可直传 (presets 响应带).
function syncFromCurrent(cur) {
  const sel = document.getElementById("source-select");
  const inp = document.getElementById("source-input");
  const apply = (c) => {
    if (!sel || !inp) return;
    // 下拉有匹配 option → 选中并清 input; 否则清下拉选中 + 填 input (让用户看到推断后真实 source)
    const has = c && [...sel.options].some(o => o.value === c);
    if (has) { sel.value = c; inp.value = ""; }
    else { sel.selectedIndex = -1; if (c) inp.value = c; }
  };
  if (cur !== undefined && cur !== null) { apply(cur); return; }
  fetch("/api/source").then(r => r.json()).then(d => apply(d.current || "")).catch(() => {});
}

function initPresets() {
  // GET /api/presets → 填充 #source-select 友好名选项 (全部历史 session / 实时编排 / 各 project)
  const sel = document.getElementById("source-select");
  if (!sel) return;
  fetch("/api/presets").then(r => r.json()).then(d => {
    sel.innerHTML = d.presets.map(p =>
      `<option value="${esc(p.source)}">${esc(p.label)}</option>`).join("");
    syncFromCurrent(d.current || "");
  }).catch(() => {});
}

function initSourceSwitcher() {
  const sel = document.getElementById("source-select");
  const inp = document.getElementById("source-input");
  const btn = document.getElementById("source-apply");
  if (!sel || !inp || !btn) return;
  // 收起所有 drill 视图 (session/spawn/turn) 回 fleet: 切源后 drill 上下文 (sid/agentId) 已失效
  const resetToFleet = () => {
    ["level2-view", "level3-view", "level4-view"].forEach(id =>
      document.getElementById(id).classList.add("hidden"));
    document.getElementById("fleet-view").classList.remove("hidden");
    _skillExpandedName = null;   // 切源 → skill 上下文失效, 清展开名 (新源同名 skill 不误展开)
  };
  const applySource = (src) => {
    if (!src) { alert("数据源不能为空"); return; }
    fetch("/api/source", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({source: src})
    }).then(r => r.json().then(body => ({ok: r.ok, body})))
      .then(({ok, body}) => {
        if (ok) {                        // 切源成功 → 回 fleet-view 重拉新源
          resetToFleet();
          fetch("/api/result").then(rr => rr.json()).then(render).catch(() => {});
          syncFromCurrent(body.current);   // body.current 是 server 推断后真实 source (带 prefix)
        } else {                          // 400 非法 / 503 refresh 失败 (server 已回滚) → 报错 + 回填
          alert("切源失败: " + (body.error || "unknown"));
          syncFromCurrent();
        }
      }).catch(e => alert("切源错误: " + e));
  };
  sel.addEventListener("change", () => applySource(sel.value));   // 选下拉项 → 直接切 (无 __custom__ 特判)
  btn.addEventListener("click", () => applySource(inp.value.trim()));
}

function initBrowser() {
  const modal = document.getElementById("browse-modal");
  const pathEl = document.getElementById("browse-path");
  const listEl = document.getElementById("browse-list");
  const inp = document.getElementById("source-input");
  const sel = document.getElementById("source-select");
  const btnBrowse = document.getElementById("source-browse");
  if (!modal || !pathEl || !listEl || !inp) return;

  let curDir = null, selectedPath = null;   // 当前浏览目录 / 选中文件绝对路径 (null=未选 → 选定用 curDir)
  const open = () => { modal.classList.remove("hidden"); load(null); };
  const close = () => { modal.classList.add("hidden"); };
  const load = (dir) => {
    const url = "/api/browse" + (dir ? "?dir=" + encodeURIComponent(dir) : "");
    fetch(url).then(r => r.json()).then(d => {
      if (d.error) { alert("浏览失败: " + d.error); return; }
      curDir = d.dir;
      selectedPath = null;
      pathEl.textContent = d.dir;
      pathEl.title = d.dir;
      renderList(d);
    }).catch(e => alert("浏览错误: " + e));
  };
  const renderList = (d) => {
    listEl.innerHTML = "";
    if (d.parent !== null && d.parent !== undefined) {
      const up = document.createElement("div");
      up.className = "browse-item browse-up";
      up.textContent = "📁 .. (返回上级)";
      up.onclick = () => load(d.parent);
      listEl.appendChild(up);
    }
    d.entries.forEach(e => {
      const row = document.createElement("div");
      row.className = "browse-item" + (e.isDir ? " is-dir" : " is-file");
      row.textContent = (e.isDir ? "📁 " : "📄 ") + e.name;
      const full = d.dir.endsWith("/") ? d.dir + e.name : d.dir + "/" + e.name;
      if (e.isDir) {
        row.onclick = () => load(full);                 // 目录 → 钻进去
      } else {
        row.onclick = () => {                            // 文件 → 选中 (类型由 server 推断, 前端不预设 prefix)
          listEl.querySelectorAll(".browse-item.sel").forEach(el => el.classList.remove("sel"));
          row.classList.add("sel");
          selectedPath = full;
        };
      }
      listEl.appendChild(row);
    });
  };
  const choose = () => {
    const target = selectedPath || curDir;   // 文件 → 文件路径; 否则当前目录 (server _infer_source 自动推断类型)
    if (!target) { alert("未选定"); return; }
    inp.value = target;                       // 裸 path, 无 prefix (server 推断)
    if (sel) sel.selectedIndex = -1;          // 清下拉选中 (裸 path 不匹配任何预置项)
    close();
    inp.focus();
  };
  if (btnBrowse) btnBrowse.onclick = open;
  const bx = document.getElementById("browse-x");
  const bc = document.getElementById("browse-cancel");
  const bs = document.getElementById("browse-select");
  if (bx) bx.onclick = close;
  if (bc) bc.onclick = close;
  if (bs) bs.onclick = choose;
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && !modal.classList.contains("hidden")) close();
  });
}

// hero 面板行点击: 委托到稳定父级 (#hero-cache-body/#hero-context-body 元素持久, 仅 innerHTML 每轮重置 → 一次性挂载)
function initHeroClicks() {
  for (const id of ["hero-cache-body", "hero-context-body"]) {
    const host = document.getElementById(id);
    if (!host) continue;
    host.addEventListener("click", (e) => {
      const row = e.target.closest(".dist-row");
      if (!row) return;
      if (row.dataset.sid) { drillSession(row.dataset.sid); return; }   // session 柱 → 进 session 视图
      if (row.classList.contains("dist-more")) jumpToFleetTable();       // '…N more' → 跳总览表
    });
  }
}
// '…N more' → 滚到下方 fleet 总览表 + 闪一下 (非原地展开; hero=聚光灯, 表=花名册, 分工不破)
function jumpToFleetTable() {
  const tbl = document.getElementById("fleet-table");
  if (!tbl) return;
  tbl.scrollIntoView({ behavior: "smooth", block: "start" });
  tbl.classList.add("flash");
  setTimeout(() => tbl.classList.remove("flash"), 1200);
}
// cache 书挡 …N more → 滚到下方 agents 面板 + 闪 (镜像 jumpToFleetTable; 书挡=聚光灯, agents 面板=花名册, 分工对称)
function jumpToAgentsPanel() {
  const pan = document.getElementById("agents-panel");
  if (!pan) return;
  pan.scrollIntoView({ behavior: "smooth", block: "start" });
  pan.classList.add("flash");
  setTimeout(() => pan.classList.remove("flash"), 1200);
}

initHeroClicks();
initLiveTail();
initTheme();
initPresets();
initSourceSwitcher();
initBrowser();
initSort();
fetch("/api/result").then(r => r.json()).then(render)

  .catch(e => { document.getElementById("meta").textContent = "load failed: " + e; });
