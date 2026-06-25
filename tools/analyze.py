"""Offline reader — Mode A: 喂插件自有 JSONL (§9 graduation criterion 3).

把 agent-insight recorder 落盘的 JSONL (§6 schema) 读回来, 在 IR 层重建:
  - per-subagent token 账 (grandTotal + bySubagentType, §7 生成视图);
  - 调用拓扑 (parentType / callChain / trigger), §7 路径A 无状态、agent_id 离线链接;
  - 自洽诊断 (isRoot 不变量 / orphan caller / null spawned / 环检测).

「自包含」判据 (毕业判据 3, AGENTS.md): reader 只读插件自己产的 JSONL, 不依赖任何 CC transcript
格式, 即可还原完整 token 账 + 拓扑, 且与 recorder 落盘的事实自洽 (无数据丢失 / 无误归因).

§9 双数据源架构 —— 本文件实现 own-JSONL 通路:
    own-JSONL ──load──► record(§6) ──to_event──► Event IR(§9.1) ──build_topology──► 派生视图
                                                                      │
                                                    聚合 / CLI / (未来) §8 dashboard
  - to_event(): §6 record → §9.1 Event IR. own-JSONL 通路下 record 已是 §6 形状, 投影即 Event.
  - build_topology(): §7 路径A, 按 sessionId 分组 + agent_id 离线链接, 派生 parentType/callChain/trigger.
  - OfflineAdapter (Mode B: 喂 CC session transcript, §9.2·B) 已交付 tools/transcript_adapter.py:
    解析 root <session>.jsonl 的 Agent `toolUseResult` + <session>/subagents/agent-<agentId>.jsonl,
    复用同一 to_event / build_topology / CLI, 仅换 ingest 入口 (--transcript flag). 平台边界 (§9.3):
    CC transcript 只持久化 root 直发结构化 spawn → Mode B 只重建 depth-2 (root→agent),
    depth-3+ token/拓扑须靠 live hook.

离线边界 (§9.3, reader 文档须标):
  1. 本 reader (Mode A) 读的是插件自己落盘的 JSONL —— 完整性取决于 hook 是否在场捕获;
     hook 未开的 session (foreign) 这里读不到, 须走 Mode B (transcript adapter).
  2. agent_id 是 session 内作用域 —— 拓扑按 sessionId 分组重建, 不跨 session
     (跨 session 缝合靠 generationId, §10.1; 本 reader 只做 session 内树).
  3. orphan caller = caller.agentId 在本 session 数据里找不到对应 spawned 记录
     (嵌套内层未被捕获 / 数据不全) —— 标 orphan, 不阻断、不误归因 (数据完整性注记, 非一致性违例).

用法:
    python3 tools/analyze.py                       # 扫默认 logdir 下全部 project
    python3 tools/analyze.py --project demo-project
    python3 tools/analyze.py --jsonl path/to/YYYY-MM-DD.jsonl
    python3 tools/analyze.py --tree                # 逐条调用链表
    python3 tools/analyze.py --json                # 机器可读 (测试 / 下游 / §8 dashboard)
"""
import argparse
import glob
import json
import os
import sys
import time
import datetime
from collections import defaultdict, Counter
from types import SimpleNamespace

try:
    from transcript_adapter import load_transcript, discover_root_transcripts, root_context_samples, count_ctx_limit_errors, count_tool_errors   # Mode B (§9.2·B) + Plan 3a root-context 通道 + §8.3 💥 爆掉事件通道 + §8.6 ✗ tool 失败定位通道; 缺失则 Mode A 照跑
except ImportError:
    load_transcript = None
    discover_root_transcripts = None
    root_context_samples = None
    count_ctx_limit_errors = None
    count_tool_errors = None

try:
    from terminal_stats import terminal_stats as _terminal_stats   # 单一计费口径源 (offline+live 共用, 见 terminal_stats.py); live 读端补全用
except ImportError:
    _terminal_stats = None

try:
    from budget import _budget_threshold, _budget_state   # 预算判定单一源头 (2026-06-24 抽离; reader 离线 + recorder 实时 emission 共用, 见 budget.py)
except ImportError:
    _budget_threshold = lambda: None
    _budget_state = lambda cumulative, threshold: None

DEFAULT_LOGDIR = os.path.expanduser("~/.claude/agent-insight")
DEFAULT_PROJECTS = os.path.expanduser("~/.claude/projects")
_TOK_KEYS = ["input", "output", "cacheCreation", "cacheRead", "total"]
_DEPTH2_NOTE = ("Mode B 恒重建 depth-2: CC transcript 只持久化 root 直发结构化 "
                "toolUseResult, 嵌套调用该行 toolUseResult=null → depth-3+ token/拓扑须 live hook.")


# ---------- ingest: own-JSONL → record ----------
def _load_jsonl_file(path):
    """读一个 JSONL 文件, 返回 (records, skipped). 跳过空行 / 非法 JSON / 非 dict 行."""
    records, skipped = [], 0
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    skipped += 1
                    continue
                if isinstance(obj, dict):
                    records.append(obj)
                else:
                    skipped += 1
    except FileNotFoundError:
        pass
    return records, skipped


def load_records(args):
    """按 args 收集 records (跨多文件 / 多 project). 返回 (records, files_read, skipped_total)."""
    records, files, skipped = [], 0, 0
    if args.jsonl:
        rs, sk = _load_jsonl_file(args.jsonl)
        return records + rs, 1, sk
    logdir = args.logdir or DEFAULT_LOGDIR
    if not os.path.isdir(logdir):
        return records, files, skipped
    projects = [args.project] if args.project else sorted(os.listdir(logdir))
    for proj in projects:
        pdir = os.path.join(logdir, proj)
        if not os.path.isdir(pdir):
            continue
        for fname in sorted(os.listdir(pdir)):
            if not fname.endswith(".jsonl") or fname.endswith(".lock"):
                continue
            if args.since and fname < (args.since + ".jsonl"):
                continue
            rs, sk = _load_jsonl_file(os.path.join(pdir, fname))
            records += rs
            skipped += sk
            files += 1
    return records, files, skipped


def _reconcile_live_records(records, projects_root=None):
    """读端补全 (live 专用): tokenSource != "agentFile" 的 SubagentCall 记录 → 扫其 subagents/agent-<id>.jsonl,
    用 terminal_stats 终态累计覆盖 token (单一计费口径, 与 record.py / transcript_adapter 同核).

    为何需要 (2026-06-19, 与 record.py 同根缺陷): 1) async_launched 记录 PostToolUse 时 agent 文件未写完
    (usage 恒 None, capturePhase=launch); 2) 历史 JSONL (record.py 修正前写入) 携末轮 usage, 1.7x-17x 低估,
    且无 tokenSource 字段. agent 一旦完成, 文件落定 → 读端按 sessionId glob 到 agent 文件即可补真值.
    dashboard 是 passive reader (刷新即最新), 读端补全 = 与 sweep hook 同结果但零编排开销 / 零状态文件 / 零重复写.

    每 agentId 仅一条 PostToolUse 记录 → 覆盖到位即可, 不需去重. terminal_stats 不可用 / 无文件 / 异常
    → 占位不动 (回退末轮/None). bulletproof: 全程 swallow (红线: 观测绝不阻断).
    projects_root: 默认 ~/.claude/projects (生产); 测试传 tmp dir."""
    if not _terminal_stats:
        return records
    # 1) 收集需补的 (sessionId → agentId 集合); 已 agentFile 的跳过 (省 terminal_stats 读盘)
    by_sid = defaultdict(set)
    for r in records:
        if r.get("recordType") != "SubagentCall":
            continue
        if r.get("tokenSource") == "agentFile":
            continue
        sid = r.get("sessionId")
        aid = (r.get("spawned") or {}).get("agentId")
        if sid and aid:
            by_sid[sid].add(aid)
    if not by_sid:
        return records
    # 2) 每 session glob 一次 subagents/, 建 (sid, aid) → 终态累计表
    #    proot 优先级: 显式参 (测试) > env AGENTINSIGHT_PROJECTS_ROOT (非标准 CC home) > ~/.claude/projects
    proot = projects_root or os.environ.get("AGENTINSIGHT_PROJECTS_ROOT") \
            or os.path.expanduser("~/.claude/projects")
    term = {}
    for sid, aids in by_sid.items():
        for cand in glob.glob(os.path.join(proot, "*", sid, "subagents", "agent-*.jsonl")):
            base = os.path.basename(cand)
            if ".meta." in base:                       # 排除 meta 旁车文件
                continue
            aid = base[len("agent-"):-len(".jsonl")]
            if aid not in aids:
                continue
            try:
                m, u = _terminal_stats(cand)
            except Exception:
                m, u = None, None
            if u and (u["cacheRead"] + u["cacheCreation"] + u["input"]) > 0:
                term[(sid, aid)] = (m, u)
    if not term:
        return records
    # 3) 就地覆盖 token / tokenSource / capturePhase (resolvedModel 缺则补)
    for r in records:
        if r.get("recordType") != "SubagentCall" or r.get("tokenSource") == "agentFile":
            continue
        key = (r.get("sessionId"), (r.get("spawned") or {}).get("agentId"))
        hit = term.get(key)
        if not hit:
            continue
        m, u = hit
        r["tokens"] = {
            "input": u["input"], "output": u["output"],
            "cacheCreation": u["cacheCreation"], "cacheRead": u["cacheRead"],
            "total": (u["input"] + u["output"] + u["cacheCreation"] + u["cacheRead"]) or None,
        }
        r["tokenSource"] = "agentFile"
        r["capturePhase"] = "complete"
        if m and not r.get("resolvedModel"):
            r["resolvedModel"] = m
    return records


# ---------- §9.1 Event IR ----------
def to_event(rec):
    """§6 record → §9.1 Event IR (own-JSONL 通路: record 已 §6 形状, 投影即 Event).
    非 SubagentCall (Skill/Command) 的 token/spawned 留空, 只参与 by-track 计数."""
    caller = rec.get("caller") or {}
    spawned = rec.get("spawned") or {}
    return {
        "recordType": rec.get("recordType"),
        "sessionId": rec.get("sessionId"),
        "generationId": rec.get("generationId"),
        "runId": rec.get("runId"),
        "toolUseId": rec.get("toolUseId"),
        "timestamp": rec.get("timestamp"),
        "subagentType": rec.get("subagentType"),
        "callerAgentId": caller.get("agentId"),
        "callerType": caller.get("agentType"),
        "isRoot": caller.get("isRoot"),
        "spawnedAgentId": spawned.get("agentId"),
        "tokens": rec.get("tokens") or {},
        "durationMs": rec.get("durationMs"),
        "resolvedModel": rec.get("resolvedModel"),
        "success": rec.get("success"),
        "status": rec.get("status"),     # 原始 status (completed/async_launched/...) — 前端健康信号区分异步后台 (2026-06-19)
        "capturePhase": rec.get("capturePhase"),   # launch/complete — reconcile 补全标记 (complete = agent jsonl 终态已落); asyncCount 据此排除已结束的 async spawn
        "skillName": rec.get("skillName"),     # SkillCall only (§8.11); SubagentCall/Command → None
        "callerTurn": rec.get("callerTurn"),   # D6: 含该 Skill tool_use 的 assistant 行序号 (drillTurn 锚点); 非 SkillCall → None
    }


# ---------- §7 路径A 拓扑重建 ----------
def _walk_chain(ev, spawned_map):
    """沿 caller 链回溯到根, 返回 (chain, orphan).
    chain: root→…→本条; 达根则前置 'orchestrator' 角色标签 (决策3: 非写死 agent 名).
    orphan=True: caller 链中途断 (caller 未在本 session 数据里被 spawned / 环), 未达根."""
    types = []
    cur = ev
    seen = set()
    reached_root = False
    while True:
        types.append(cur.get("subagentType"))
        caller = cur.get("callerAgentId")
        if caller is None:
            reached_root = True
            break
        if caller in seen:
            break  # 环检测 (理论上不该出现)
        seen.add(caller)
        parent = spawned_map.get(caller)
        if parent is None:
            break  # orphan: caller 未被本 session 任何记录 spawned
        cur = parent
    types.reverse()
    if reached_root:
        types = ["orchestrator"] + types
    return types, (not reached_root)


def build_topology(events):
    """§7 路径A: 按 sessionId 分组, 用 agent_id 离线链接派生 parentType/callChain/trigger.
    events: SubagentCall Event 列表. 返回每条附 {parentType, callChain, trigger, depth, orphan}."""
    out = []
    by_session = defaultdict(list)
    for ev in events:
        by_session[ev.get("sessionId")].append(ev)
    for sess in by_session.values():
        # spawned_map: spawnedAgentId -> ev (每个 agentId 被 spawn 一次, 1:1)
        spawned_map = {}
        for ev in sess:
            sa = ev.get("spawnedAgentId")
            if sa:
                spawned_map[sa] = ev
        for ev in sess:
            caller = ev.get("callerAgentId")
            if caller is None:
                parent_type, trigger = "orchestrator", "root"
            else:
                parent = spawned_map.get(caller)
                parent_type = parent.get("subagentType") if parent else None
                trigger = "subagent"
            chain, orphan = _walk_chain(ev, spawned_map)
            d = dict(ev)
            d["parentType"] = parent_type
            d["trigger"] = trigger
            d["callChain"] = chain
            d["depth"] = len(chain)
            d["orphan"] = orphan
            out.append(d)
    out.sort(key=lambda e: (e.get("timestamp") or "", e.get("toolUseId") or ""))
    return out


# ---------- 聚合视图 (§7 生成视图) ----------
def _tok(t, k):
    v = (t or {}).get(k)
    return v if isinstance(v, (int, float)) else 0


def grand_total(events):
    agg = {k: 0 for k in _TOK_KEYS}
    for ev in events:
        for k in _TOK_KEYS:
            agg[k] += _tok(ev.get("tokens"), k)
    return agg


def by_subagent_type(events, _raw=False):
    grp = defaultdict(lambda: {"calls": 0, "input": 0, "output": 0, "cacheCreation": 0,
                               "cacheRead": 0, "total": 0, "durSum": 0, "durN": 0, "success": 0})
    for ev in events:
        t = ev.get("subagentType") or "unknown"
        g = grp[t]
        g["calls"] += 1
        for k in ["input", "output", "cacheCreation", "cacheRead", "total"]:
            g[k] += _tok(ev.get("tokens"), k)
        dm = ev.get("durationMs")
        if isinstance(dm, (int, float)):
            g["durSum"] += dm
            g["durN"] += 1
        if ev.get("success"):
            g["success"] += 1
    rows = []
    for t, g in grp.items():
        row = {
            "subagentType": t, "calls": g["calls"],
            "input": g["input"], "output": g["output"],
            "cacheCreation": g["cacheCreation"], "cacheRead": g["cacheRead"],
            "total": g["total"],
            "avgDurationMs": round(g["durSum"] / g["durN"], 1) if g["durN"] else None,
            "successRate": round(g["success"] / g["calls"], 3) if g["calls"] else None,
        }
        # _raw: 暴露原始累加器, 供跨 session 合并时重算率 (率不能平均平均, §13 scan). 默认关 → Mode A/B 输出零变化.
        if _raw:
            row["durSum"] = g["durSum"]
            row["durN"] = g["durN"]
            row["successCount"] = g["success"]
        rows.append(row)
    rows.sort(key=lambda r: r["total"], reverse=True)
    return rows


def _ts_to_ms(ts):
    """ISO 8601 timestamp → epoch ms (与 JS Date.parse 同口径).
    用于 spawn 时序排序: skill 切面 turns 的 #i 与 session 页 segs 共用同一时序序号 (app.js L491-493).
    bulletproof: 无法解析 → None (roster 跳过该项, 不阻断观测)."""
    if not ts or not isinstance(ts, str):
        return None
    try:
        s = ts[:-1] + "+00:00" if ts.endswith("Z") else ts   # py3.10 fromisoformat 不认 'Z'
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)   # 无 tz 当 UTC (与 JS 同; CC ts 恒带 Z/offset)
        return dt.timestamp() * 1000.0
    except Exception:
        return None


def by_skill(events):
    """§8.11 skill 活跃度表 + skill×caller 共现 (零 token F3, 故无 token 列).

    输入: 全部 Event (筛 SkillCall). caller 共现零代价——每 SkillCall 已带 caller.
    callerType 解析 (offline caller.agentType=None 故需补): 直接字段 (live 已填) >
    callerAgentId 查 spawned_type 映射 (offline, 子 agent 内调 skill 归到该 agent 的 subagentType) >
    'orchestrator' (root 直发).

    字段 (§8.11 三子视图):
      calls/sessions       — 活跃度;
      spawns               — distinct callerAgentId (非 None; root 直发不计), 子视图1;
      sessionIds/spawnIds  — 回链锚点 (子视图3 点 skill → 它的 session/spawn 列表), sorted list.
    """
    # spawn 时间序 roster: 复刻前端 segs (start=ts-dur 升序的 #i, app.js L483-493) —— turns 带 spawnIdx,
    # 与 session 页 agents 行徽章 / 时间轴 tooltip / 异步竖线 共用同一时序序号, 全局可互指 (设计 §L491 注释).
    _roster = defaultdict(list)   # sid -> [(start_key, spawnedAgentId)]
    for _ev in events:
        if _ev.get("recordType") != "SubagentCall":
            continue
        _sid = _ev.get("sessionId"); _sa = _ev.get("spawnedAgentId")
        _ms = _ts_to_ms(_ev.get("timestamp"))
        if not (_sid and _sa and _ms is not None):
            continue
        _dur = _ev.get("durationMs")
        _dur = _dur if isinstance(_dur, (int, float)) else 0
        _roster[_sid].append((_ms - _dur, _sa))
    _spawn_idx = {}   # (sid, spawnedAgentId) -> #i
    for _sid, _lst in _roster.items():
        _lst.sort(key=lambda x: x[0])   # 升序; 同 start 并发相对序任意 (用户认可, app.js L492)
        for _i, (_k, _sa) in enumerate(_lst):
            _spawn_idx[(_sid, _sa)] = _i
    spawned_type = {ev.get("spawnedAgentId"): ev.get("subagentType")
                    for ev in events
                    if ev.get("recordType") == "SubagentCall" and ev.get("spawnedAgentId")}
    grp = defaultdict(lambda: {"calls": 0, "sessions": set(), "spawns": set(),
                               "callers": defaultdict(int), "turns": []})
    for ev in events:
        if ev.get("recordType") != "SkillCall":
            continue
        name = ev.get("skillName") or "unknown"
        g = grp[name]
        g["calls"] += 1
        if ev.get("sessionId"):
            g["sessions"].add(ev["sessionId"])
        aid = ev.get("callerAgentId")
        if aid:                                   # root 直发 (None) 不计 spawn — §8.11 子视图1
            g["spawns"].add(aid)
        ct = (ev.get("callerType") or spawned_type.get(aid) or "orchestrator")
        g["callers"][ct] += 1
        _turn = ev.get("callerTurn")
        if _turn is not None:                      # D7: turn 锚点 (drillTurn 用); None 不进. 带 sessionId/agentType → 主面板 skill 行 inline 展开 session/spawn/turn 定位.
            g["turns"].append({"sessionId": ev.get("sessionId"), "agentId": aid,
                               "spawnIdx": _spawn_idx.get((ev.get("sessionId"), aid)),   # 与 session 页 #i 同口径 (start=ts-dur 升序位次); None=root 直发/未补全 → 点行可互指时间轴
                               "agentType": ct, "turn": _turn})
    rows = []
    for name, g in grp.items():
        rows.append({
            "skillName": name, "calls": g["calls"], "sessions": len(g["sessions"]),
            "spawns": len(g["spawns"]),
            "sessionIds": sorted(g["sessions"]), "spawnIds": sorted(g["spawns"]),
            "callerTypes": dict(g["callers"]),   # skill × callerType 共现 (§8.11 子视图2)
            "turns": g["turns"],                 # D7: 每次调用 (callerAgentId, turn) — 详情页 turn chip; fleet 合并不渲染
        })
    rows.sort(key=lambda r: r["calls"], reverse=True)
    return rows


def _per_session_row(*, project, sid, spawns, grand_total_dict, dur_ms,
                     consistent, mode_label, ctx_peak=0, ctx_limit_errors=None,
                     root_usage=None, async_count=0, tool_error_count=0,
                     generation_id=None):
    """Build one perSession row — app.js fleet-table 契约 15 字段 (§9 双数据源: live+offline 同形渲染).
    Shared by Mode A (live logdir, 按 sessionId 分组) 与 Mode B (scan, per mini-result).
    值由调用方算好; 本函数只 shape → 两源形状逐字段一致, 不漂移.
    generation_id: Phase 3 跨 session 续接 (§10.1); 缺/无 carrier → = sid (singleton, 今天行为)."""
    total = grand_total_dict["total"]
    return {
        "project": project,
        "sid": sid,
        "generationId": generation_id,   # Phase 3 (§10.1): = sid 当无 carrier/lineage; fleet gen-tag 显 ⟿ 当 ≠ sid
        "spawns": spawns,
        "totalTokens": total,
        "cacheReadPct": round(_cache_hit_rate(grand_total_dict) * 100, 1),   # §8.3/红线6 命中率: cacheRead/(cacheRead+input+cacheCreation), output 不进分母 (与 dashboard app.js sessHit 同口径, 2026-06-23 定调); den=0 → 0.0
        "durationS": round(dur_ms / 1000, 1) if dur_ms else 0.0,
        "consistent": consistent,
        "modeLabel": mode_label,
        "grandTotal": grand_total_dict,
        "ctxPeak": ctx_peak,
        "ctxLimitErrors": ctx_limit_errors or {"count": 0, "sample": None},   # §8.3 💥 爆掉事件 (transcript 源: count_ctx_limit_errors; live/jsonl 源无 → count 0). app.js ctxCell 三态: count>0 → 💥
        "asyncCount": async_count,   # §8.3 异步未回报 · 真在飞 (status==async_launched 且 capturePhase!=complete; reconcile 已把 agent jsonl 终态落定的 spawn 补 complete 排除, 故 agent 结束后不再计入). app.js banner/健康列第三类异常.
        "toolErrorCount": tool_error_count,   # §8.6 ✗ tool 失败 (root 主线 tool_result is_error 计数; 与 status 分轨, 不并 successRate/grandTotal). per-spawn 部分随 callChains[].toolErrorCount. app.js 详情页健康区/花名册 ✗.
        "rootUsage": root_usage or {"input": 0, "cacheCreation": 0, "cacheRead": 0},   # §7 计费口径 root 主线逐 turn 真实计费 sum (2026-06-19 定调; 与 grandTotal 同公式); app.js sessHit 把它与 grandTotal 合并算统一 cache 命中率 (纯 root session 不再显 —)
    }


def call_graph(topo):
    """(parentType → childType) 边, 权重=观测次数. orphan (caller 未解析) 归入 '(unresolved)'."""
    edges = defaultdict(int)
    for d in topo:
        p = d.get("parentType") or "(unresolved)"
        c = d.get("subagentType") or "unknown"
        edges[(p, c)] += 1
    return [{"parentType": p, "childType": c, "count": n}
            for (p, c), n in sorted(edges.items(), key=lambda kv: -kv[1])]


# ---------- 自洽诊断 (§9.4 「与运行时自洽」) ----------
def consistency(events, topo):
    """recorder 不变量交叉校验 + 数据完整性注记.
    consistent=True 仅看 isRoot 不变量 (caller.agentId None ⇔ isRoot True, record.py:122 同源);
    orphan / null 是数据完整性注记 (不完整捕获, §9.3 caveat), 不计入一致性违例."""
    isroot_bad = [ev.get("toolUseId") for ev in events
                  if (ev.get("callerAgentId") is None) != (ev.get("isRoot") is True)]
    null_spawned = sum(1 for ev in events if ev.get("spawnedAgentId") is None)
    null_tokens = sum(1 for ev in events
                      if not ev.get("tokens") or ev.get("tokens", {}).get("total") is None)
    orphan_callers = sorted({d.get("callerAgentId") for d in topo
                             if d.get("orphan") and d.get("callerAgentId")})
    return {
        "consistent": len(isroot_bad) == 0,
        "isRootInvariantViolations": isroot_bad,
        "nullSpawned": null_spawned,
        "nullTokens": null_tokens,
        "orphanChains": sum(1 for d in topo if d.get("orphan")),  # 数据完整性注记 (非 bug)
        "orphanCallerIds": orphan_callers,
    }


# ---------- 渲染 ----------
def _col(v, w):
    """右对齐一列; None → '-'. (值转字符串后再对齐, 统一处理数字 / None.)"""
    return ("-" if v is None else str(v)).rjust(w)


def render_skill(rows):
    """§8.11 by-skill 切面 (C 形态). 零 token F3 → 只活跃度 + caller 共现, 无 token 列."""
    if not rows:
        return
    print("\nskill 活跃度 (by-skill 切面, §8.11 · 零 token F3):")
    print(f"  {'skillName':<34}{'calls':>6}{'sessions':>9}{'spawns':>8}  caller 共现")
    for r in rows:
        co = ", ".join(f"{k} ×{v}" for k, v in sorted(r["callerTypes"].items(), key=lambda kv: -kv[1]))
        print(f"  {(r['skillName'] or 'unknown'):<34}{r['calls']:>6}{r['sessions']:>9}"
              f"{r.get('spawns', 0):>8}  {co}")


def render(res, topo, show_tree):
    bt = res["byTrack"]
    print(f"agent-insight offline reader  (Mode {res.get('modeLabel', 'A · own-JSONL')}, §9)")
    print(f"files: {res['files']}   records: {res['recordsTotal']} "
          f"(SubagentCall {bt.get('SubagentCall', 0)} / SkillCall {bt.get('SkillCall', 0)} / "
          f"Command {bt.get('Command', 0)})   sessions: {len(res['sessions'])}   "
          f"skipped-bad-lines: {res['skippedBadLines']}")
    gt = res["grandTotal"]
    print(f"\ngrand total tokens: input={gt['input']} output={gt['output']} "
          f"cacheCreation={gt['cacheCreation']} cacheRead={gt['cacheRead']} total={gt['total']}")
    rows = res["bySubagentType"]
    if rows:
        print("\nper subagentType (按 total 降序):")
        print(f"  {'subagentType':<22}{'calls':>6}{'total':>10}{'input':>9}"
              f"{'output':>8}{'cacheRead':>10}{'avgDur':>10}{'success':>9}")
        for r in rows:
            print(f"  {(r['subagentType'] or 'unknown'):<22}{r['calls']:>6}{r['total']:>10}"
                  f"{r['input']:>9}{r['output']:>8}{r['cacheRead']:>10}"
                  f"{_col(r['avgDurationMs'], 10)}{_col(r['successRate'], 9)}")
    render_skill(res.get("bySkill", []))
    cg = res["callGraph"]
    if cg:
        print("\ncall graph (parentType → childType · 次数):")
        for e in cg:
            print(f"  {e['parentType']} → {e['childType']}   x{e['count']}")
    c = res["consistency"]
    flag = "✅ consistent" if c["consistent"] else "❌ inconsistent"
    print(f"\nself-consistency: {flag}  (isRoot 不变量违例: {len(c['isRootInvariantViolations'])})")
    if c["orphanChains"]:
        ids = ", ".join(c["orphanCallerIds"]) if c["orphanCallerIds"] else "(无)"
        print(f"  orphan chains: {c['orphanChains']}  (caller 未在本 session 捕获 — 嵌套内层未录, §9.3 caveat; 非一致性违例)")
        print(f"  orphan caller ids: {ids}")
    if c["nullSpawned"]:
        print(f"  null spawned: {c['nullSpawned']}  (spawned.agentId 缺, 无法链接)")
    if c["nullTokens"]:
        print(f"  null tokens:  {c['nullTokens']}  (token 未捕获)")
    if show_tree and topo:
        print("\ncall chains (按时间序):")
        print(f"  {'ts':<26}{'session':<14}{'subagentType':<16}{'parentType':<15}"
              f"{'trigger':<9}{'depth':>6}{'total':>9}{'dur':>8}  chain")
        for d in topo:
            sid = (d.get("sessionId") or "-")[:13]
            st = (d.get("subagentType") or "-")[:15]
            pt = (d.get("parentType") or "-")[:14]
            tr = d.get("trigger") or "-"
            depth = d.get("depth", "-")
            tot = _tok(d.get("tokens"), "total")
            chain = " → ".join(d.get("callChain") or [])
            tag = "  [orphan]" if d.get("orphan") else ""
            print(f"  {(d.get('timestamp') or '-'):<26}{sid:<14}{st:<16}{pt:<15}"
                  f"{tr:<9}{str(depth):>6}{str(tot):>9}{_col(d.get('durationMs'), 8)}  {chain}{tag}")


# ---------- Mode B · scan-projects (fleet-wide 离线观测, §9.2·B) ----------
def _merge_grand_total(rows):
    """各 session grandTotal dict 各 token 项求和 → 一个 dict."""
    agg = {k: 0 for k in _TOK_KEYS}
    for r in rows:
        for k in _TOK_KEYS:
            agg[k] += (r.get(k) or 0)
    return agg


def _merge_by_type(rows):
    """_raw by-subagent-type 行跨 session 合并: 绝对量求和, 率用原始累加器重算 (率不能平均平均, §13 scan)."""
    agg = defaultdict(lambda: {"calls": 0, "input": 0, "output": 0, "cacheCreation": 0,
                               "cacheRead": 0, "total": 0, "durSum": 0, "durN": 0, "successCount": 0})
    for r in rows:
        t = r.get("subagentType") or "unknown"
        g = agg[t]
        g["calls"] += r.get("calls") or 0
        for k in ["input", "output", "cacheCreation", "cacheRead", "total"]:
            g[k] += r.get(k) or 0
        g["durSum"] += r.get("durSum") or 0
        g["durN"] += r.get("durN") or 0
        g["successCount"] += r.get("successCount") or 0
    out = []
    for t, g in agg.items():
        out.append({
            "subagentType": t, "calls": g["calls"],
            "input": g["input"], "output": g["output"],
            "cacheCreation": g["cacheCreation"], "cacheRead": g["cacheRead"],
            "total": g["total"],
            "avgDurationMs": round(g["durSum"] / g["durN"], 1) if g["durN"] else None,
            "successRate": round(g["successCount"] / g["calls"], 3) if g["calls"] else None,
            "durSum": g["durSum"], "durN": g["durN"], "successCount": g["successCount"],
        })
    out.sort(key=lambda r: r["total"], reverse=True)
    return out


def _merge_call_graph(rows):
    """边 (parentType, childType) → count 求和, 按 count 降序."""
    edges = defaultdict(int)
    for r in rows:
        edges[(r.get("parentType") or "(unresolved)", r.get("childType") or "unknown")] += r.get("count") or 0
    return [{"parentType": p, "childType": c, "count": n}
            for (p, c), n in sorted(edges.items(), key=lambda kv: -kv[1])]


def _merge_by_skill(by_skill_rows_per_session):
    """per-session by_skill() 行集列表 → fleet 合并. 每个列表 = 一个 session.

    正确性 (与 _merge_by_type 同理——sessions 是 distinct 计数, 不能加每行 sessions):
    sessions   = 该 skill 出现在几个 mini-result (一个 mini-result 调过它 = 1 distinct session);
    calls      = 各项求和;
    callerTypes = 各 caller 求和;
    spawns/sessionIds/spawnIds = 跨 session union (set, 非加 len) — CC agentId 全局唯一故 union = distinct."""
    agg = defaultdict(lambda: {"calls": 0, "sessions": 0, "spawns": 0,
                               "sessionIds": set(), "spawnIds": set(),
                               "callers": defaultdict(int), "turns": []})
    for rows in by_skill_rows_per_session:
        for r in rows:
            g = agg[r["skillName"]]
            g["sessions"] += 1
            g["calls"] += r.get("calls") or 0
            for sid in (r.get("sessionIds") or []):
                g["sessionIds"].add(sid)
            for sp in (r.get("spawnIds") or []):
                g["spawnIds"].add(sp)
            for ct, n in (r.get("callerTypes") or {}).items():
                g["callers"][ct] += n
            for t in (r.get("turns") or []):     # B: 跨 session 拼接 turns (每项 {sessionId,agentId,agentType,turn}); 主面板 skill 行 inline 展开 session/spawn/turn 定位
                g["turns"].append(t)
    out = [{"skillName": k, "calls": v["calls"], "sessions": v["sessions"],
            "spawns": len(v["spawnIds"]),
            "sessionIds": sorted(v["sessionIds"]), "spawnIds": sorted(v["spawnIds"]),
            "callerTypes": dict(v["callers"]), "turns": v["turns"]} for k, v in agg.items()]
    out.sort(key=lambda r: r["calls"], reverse=True)
    return out


def _projects_root(path):
    """从 scan 路径推 CC projects 根 (basename=='projects'): scan 可能指根 (默认 ~/.claude/projects),
    也可能指单个 project dir (.../projects/<proj>, dashboard 常用). reconcile glob 需根级
    (<root>/*/sid/subagents/agent-*.jsonl) —— 传 project-dir 会多一层 glob 致空 → async 全误报在飞.
    故 project-dir 源须上溯一层到 'projects'. 找不到 'projects' 祖先 → None (回退 reconcile 默认)."""
    p = os.path.abspath(path)
    while p and p != os.path.dirname(p):   # 逐级上溯; 先判自身 (scan 指根时即返回), 再上行
        if os.path.basename(p) == "projects":
            return p
        p = os.path.dirname(p)
    return None


# ---------- Phase 3: 跨 session 续接 (lineage 缝合, §10.1) ----------
def load_generations_map(log_base=None):
    """读 <log_base>/generations.jsonl → ({sessionId: generationId}, [raw_rows]). inert-safe.

    缺文件/坏目录 → ({}, []); 坏行逐行 try/except 跳 (镜像 _load_jsonl_file 的 per-line 容错); 非 dict 跳.
    log_base 默认走 record.py _log_base 同优先级 (AGENTINSIGHT_LOG_DIR > ~/.claude/agent-insight; 不认 CLAUDE_PLUGIN_DATA, 2026-06-23 修断链).
    last-writer-wins: 同 sessionId 多行后写覆盖前 (外部 writer 行更知真实 handoff 图, 应盖 plugin-hook 行).
    reader 只取 sessionId/generationId; timestamp(plugin-hook)/ts(external writer)/prevSessionId 读到不参与."""
    base = log_base or (os.environ.get("AGENTINSIGHT_LOG_DIR", "").strip()
                        or os.path.expanduser("~/.claude/agent-insight"))
    path = os.path.join(base, "generations.jsonl")
    mapping, raw = {}, []
    if not os.path.exists(path):
        return mapping, raw
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if not isinstance(d, dict):
                    continue
                sid = d.get("sessionId")
                gid = d.get("generationId")
                if sid and gid:
                    mapping[sid] = gid      # last-writer-wins (append 顺序: 后写盖前)
                    raw.append(d)
    except OSError:
        return {}, []
    return mapping, raw


def _apply_generation_map(records, mapping):
    """reader 级 post-ingest 缝合: records 的 sessionId 命中 lineage map → 覆盖 generationId.

    Mode B (transcript_adapter) 记录硬 generationId=sessionId/carrierSource=None (transcript 无 carrier 通路);
    命中 map 则覆盖 generationId, 并 (仅当原 carrierSource 空) 置 carrierSource='lineage-map' (reader 内存态标记,
    标 'post-ingest 从 generations.jsonl 恢复'; recorder 永不落盘此值 → schema carrierSource enum 不含).
    Mode A live 记录 generationId 已正确 (record.py 落盘时 carrier 已生效) → map 值与现值相同, no-op 确认.
    空 map → 直接返回, 零改动 (今天行为). 就地改 records (list of dict)."""
    if not mapping:
        return
    for r in records:
        sid = r.get("sessionId")
        if sid and sid in mapping:
            new_gid = mapping[sid]
            if r.get("generationId") != new_gid:
                r["generationId"] = new_gid
                if not r.get("carrierSource"):
                    r["carrierSource"] = "lineage-map"


# _budget_threshold / _budget_state 已抽至 tools/budget.py (2026-06-24, reader 离线 + recorder 实时 emission 单一源头).
# 顶部 from budget import _budget_threshold, _budget_state (try/except inert fallback).


def aggregate_generations(per_session_rows, threshold=None):
    """按 generationId 卷 per_session_rows → generation 聚合列表 (跨 session 续接可见产物, §10.1).

    每条: {generationId, sessionIds[], sessionsN, spawnsTotal, grandTotal, durationS, multiSession, budgetState?}.
    generationId==sid (无 carrier/无 lineage) 的 session → 单成员 generation (multiSession=False) → 渲染同今天.
    grandTotal 复用 _merge_grand_total (cacheRead-inclusive 计费口径, 与 grandTotal 同核; pre-existing token 债不动).
    budgetState: threshold 配了才加 key — cumulative = 该 generation 跨 session 卷起的 grandTotal.total (缺口 1, reader-computes).
    按 grandTotal.total 降序."""
    by_gen = defaultdict(list)
    for row in per_session_rows:
        gid = row.get("generationId") or row.get("sid")
        by_gen[gid].append(row)
    out = []
    for gid, rows in by_gen.items():
        gt = _merge_grand_total([r["grandTotal"] for r in rows])
        gen = {
            "generationId": gid,
            "sessionIds": sorted(r["sid"] for r in rows),
            "sessionsN": len(rows),
            "spawnsTotal": sum(r["spawns"] for r in rows),
            "grandTotal": gt,
            "durationS": round(sum(r["durationS"] for r in rows), 1),
            "multiSession": len(rows) > 1,
        }
        bs = _budget_state(gt["total"], threshold)
        if bs is not None:
            gen["budgetState"] = bs        # 缺口 1: 仅配置 threshold 时才加 key (inert — 未配则字段不存在, 渲染同今天)
        out.append(gen)
    out.sort(key=lambda g: g["grandTotal"]["total"], reverse=True)
    return out


def run_scan(args):
    """扫 scan_dir 下全部 (或 --project 过滤的) root transcript, 逐 session 跑同条 Mode B 管线得 mini-result.

    per-session try/except: 单 session 异常 swallow → errors[]、continue (scan 永不 exit 2);
    KeyboardInterrupt/SystemExit 重抛. 0-Agent session 非 error (合法零-spawn mini-result).
    返回 (per_session mini-result 列表, errors 列表)."""
    scan_dir = args.scan_projects if args.scan_projects != "__default__" else DEFAULT_PROJECTS
    project = getattr(args, "project", None)
    paths = discover_root_transcripts(scan_dir, project) if discover_root_transcripts else []
    per_session, errors = [], []
    mapping, _generations_raw = load_generations_map()   # Phase 3: 全局 lineage map (generations.jsonl; 缺→空 map→inert)
    for path in paths:
        ns = SimpleNamespace(transcript=path, project=project)   # load_transcript 只 getattr transcript/project
        try:
            recs, nf, sk = load_transcript(ns)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:
            errors.append({"path": path, "error": f"{type(e).__name__}: {e}"})
            continue
        recs = _reconcile_live_records(recs, projects_root=_projects_root(scan_dir))   # 读端补全: proot 从 scan_dir 上溯到 projects 根 (scan 可能是单 project dir, dashboard 常用); 否则默认 ~/.claude/projects 找不到非标源 (非标 project) agent 终态 → async 全误报在飞. asyncCount 据此只数真在飞的
        _apply_generation_map(recs, mapping)   # Phase 3: post-reconcile 缝合 (Mode B transcript 查 map 恢复 generationId; 空 map no-op)
        events = [to_event(r) for r in recs]
        sub_events = [e for e in events if e.get("recordType") == "SubagentCall"]
        topo = build_topology(sub_events)
        _rcs = root_context_samples(path) if root_context_samples else None   # 调一次: peak + sum 都取 (避免重复遍历 transcript; §8.3 + §7 计费口径)
        _sid = os.path.basename(path)[:-len(".jsonl")] if path.endswith(".jsonl") else os.path.basename(path)
        per_session.append({
            "project": os.path.basename(os.path.dirname(path)),
            "sid": _sid,
            "generationId": (sub_events[0].get("generationId") if sub_events else None) or mapping.get(_sid) or _sid,   # Phase 3: map 覆盖后; 0-spawn session 查 lineage map 回填 generationId (空 session 也归其 generation, 不漏缝合); foreign/无 lineage → _sid (singleton)
            "path": path,
            "ctxPeak": (_rcs or {}).get("peak", 0),   # §8.3 root 主线 context 峰值 (Plan 3a 独立通道; bulletproof)
            "rootUsage": (_rcs or {}).get("sum"),   # §7 root 主线逐 turn 真实计费 sum (2026-06-19); None = 无 root transcript (live/jsonl 源) → perSession 行 fallback {0,0,0}
            "ctxLimitErrors": (count_ctx_limit_errors(path) if count_ctx_limit_errors else {"count": 0, "sample": None}),   # §8.3 💥 爆掉事件 (限 assistant 顶层 API Error; 防 echo 假阳性)
            "toolErrorCount": (count_tool_errors(path)["count"] if count_tool_errors else 0),   # §8.6 ✗ tool 失败 (root 主线 is_error 计数; 与 status 分轨); scan perSession schema 一致 (fleet 不显)
            "asyncCount": sum(1 for e in sub_events if e.get("status") == "async_launched" and e.get("capturePhase") != "complete"),   # §8.3 异步未回报 · 真在飞 (status=launched 且 reconcile 未补到 complete = agent jsonl 终态仍未落)
            "spawns": len(sub_events),
            "files": nf,
            "skippedBadLines": sk,
            "grandTotal": grand_total(sub_events),
            "bySubagentTypeRaw": by_subagent_type(sub_events, _raw=True),
            "bySkillRaw": by_skill(events),   # §8.11: 用全部 events (spawned 映射 + SkillCall)
            "callGraph": call_graph(topo),
            "consistency": consistency(sub_events, topo),
            "modeLabel": "B · transcript",
        })
    return per_session, errors


def aggregate_scan(per_session, errors, scan_dir, project):
    """跨 session 聚合 per_session mini-result → scan result dict (Mode B · scan-projects)."""
    scanned = list(per_session)
    skipped = [m for m in scanned if m["spawns"] == 0]   # 0-Agent session (合法, 非 error)
    grand = _merge_grand_total([m["grandTotal"] for m in scanned])
    by_type = _merge_by_type([r for m in scanned for r in m["bySubagentTypeRaw"]])
    cg = _merge_call_graph([e for m in scanned for e in m["callGraph"]])
    by_skill_fleet = _merge_by_skill([m["bySkillRaw"] for m in scanned])
    total_spawns = sum(m["spawns"] for m in scanned)

    per_session_rows = [
        _per_session_row(
            project=m["project"], sid=m["sid"], spawns=m["spawns"],
            grand_total_dict=m["grandTotal"],
            dur_ms=sum(r["durSum"] for r in m["bySubagentTypeRaw"]),
            consistent=m["consistency"]["consistent"],
            mode_label=m["modeLabel"], ctx_peak=m.get("ctxPeak", 0),
            ctx_limit_errors=m.get("ctxLimitErrors"),
            root_usage=m.get("rootUsage"),
            async_count=m.get("asyncCount", 0),
            tool_error_count=m.get("toolErrorCount", 0),   # §8.6 ✗ tool 失败 (root 主线; Mode A 走 _root_tool_errors, scan 走 mini-result toolErrorCount)
            generation_id=m.get("generationId"),   # Phase 3 (§10.1): mini-result 透传 (post-map generationId)
        ) for m in scanned
    ]
    top_sessions = sorted(per_session_rows, key=lambda r: r["totalTokens"], reverse=True)[:10]
    violating = [r["sid"] for r in per_session_rows if not r["consistent"]]
    return {
        "mode": "B · scan-projects",
        "scanDir": scan_dir,
        "project": project,
        "sessionsScanned": len(scanned),
        "sessionsSkipped": len(skipped),
        "errors": errors,
        "grandTotal": grand,
        "bySubagentType": by_type,
        "bySkill": by_skill_fleet,   # §8.11 fleet skill 切面 (零 token F3)
        "callGraph": cg,
        "spawnsTotal": total_spawns,
        "perSession": per_session_rows,
        "generations": aggregate_generations(per_session_rows, _budget_threshold()),   # Phase 3 (§10.1): 同 generationId 跨 session 卷起 (multiSession=True 显续接); 全 singleton=今天行为. budgetState (缺口 1): threshold 配了才挂
        "topSessions": top_sessions,
        "scanConsistency": {"allConsistent": len(violating) == 0, "violatingSessions": violating},
        "depth2Note": _DEPTH2_NOTE,
    }


def _cache_hit_rate(gt):
    """§8.3 / 红线 6 cache 命中率 = cacheRead / (cacheRead + input + cacheCreation).
    output 永不进缓存 → 不进分母 (与 dashboard app.js sessHit/billable 同口径, 2026-06-23 定调).
    den=0 (空 / 纯 output session) → 0.0."""
    cr = gt.get("cacheRead") or 0
    den = cr + (gt.get("input") or 0) + (gt.get("cacheCreation") or 0)
    return cr / den if den else 0.0


def _fmt_pct(num, den):
    return f"{num / den * 100:.1f}" if den else "0.0"


def _fmt_dur(s):
    if not s:
        return "-"
    return f"{s / 60:.1f}m" if s >= 60 else f"{s:.0f}s"


def render_scan(res):
    print("agent-insight offline reader  (Mode B · scan-projects, §9.2·B)")
    proj = res.get("project") or "(all)"
    print(f"scan dir: {res.get('scanDir')}   project: {proj}   "
          f"sessions: {res['sessionsScanned']} scanned / {res['sessionsSkipped']} skipped / "
          f"{len(res['errors'])} errors   spawns: {res['spawnsTotal']}")
    print(f"⚠ depth-2: {_DEPTH2_NOTE}")
    rows = sorted(res["perSession"], key=lambda r: r["totalTokens"], reverse=True)
    if rows:
        print("\nper-session (sorted by total tokens desc):")
        print(f"  {'project':<22}{'sid':<14}{'spawns':>7}{'total':>13}{'cacheRead%':>12}{'dur':>9}  consistent")
        for r in rows:
            sid = (r["sid"] or "-")[:13]
            proj = (r["project"] or "-")[:22]
            cons = "✓" if r["consistent"] else "✗"
            print(f"  {proj:<22}{sid:<14}{r['spawns']:>7}{r['totalTokens']:>13}"
                  f"{r['cacheReadPct']:>11}%{_fmt_dur(r['durationS']):>9}  {cons}")
    gens = [g for g in res.get("generations", []) if g.get("multiSession")]
    if gens:
        print("\ngenerations (跨 session 续接 · multiSession, §10.1):")
        for g in gens:
            print(f"  {g['generationId']:<30}{g['sessionsN']:>3} sessions  "
                  f"spawns={g['spawnsTotal']:>5}  total={g['grandTotal']['total']:>13}  "
                  f"members: {', '.join(g['sessionIds'])}")
    gt = res["grandTotal"]
    print(f"\ncross-session totals: total={gt['total']} input={gt['input']} output={gt['output']} "
          f"cacheCreation={gt['cacheCreation']} cacheRead={gt['cacheRead']} "
          f"(hit {_fmt_pct(gt['cacheRead'], gt['cacheRead'] + gt['input'] + gt['cacheCreation'])}%)  spawns={res['spawnsTotal']}")
    bt = res["bySubagentType"]
    if bt:
        print("by subagentType:")
        print(f"  {'subagentType':<22}{'calls':>6}{'total':>12}{'avgDur':>10}{'success':>9}")
        for r in bt:
            print(f"  {(r['subagentType'] or 'unknown'):<22}{r['calls']:>6}{r['total']:>12}"
                  f"{_col(r['avgDurationMs'], 10)}{_col(r['successRate'], 9)}")
    bs = res.get("bySkill", [])
    if bs:
        print("\nby skill (§8.11 · 零 token F3):")
        print(f"  {'skillName':<34}{'calls':>6}{'sessions':>9}  caller 共现")
        for r in bs:
            co = ", ".join(f"{k} ×{v}" for k, v in sorted(r["callerTypes"].items(), key=lambda kv: -kv[1]))
            print(f"  {(r['skillName'] or 'unknown'):<34}{r['calls']:>6}{r['sessions']:>9}  {co}")
    cg = res["callGraph"]
    if cg:
        print("call graph (parentType → childType · 次数):")
        for e in cg:
            print(f"  {e['parentType']} → {e['childType']}   x{e['count']}")
    sc = res["scanConsistency"]
    flag = "✅ all consistent" if sc["allConsistent"] else f"❌ {len(sc['violatingSessions'])} sessions with violations"
    print(f"\nscan self-consistency: {flag}")
    if sc["violatingSessions"]:
        print(f"  violating sessions: {', '.join(sc['violatingSessions'][:10])}")
    if res["errors"]:
        print(f"\nerrors ({len(res['errors'])}):")
        for e in res["errors"][:10]:
            print(f"  {os.path.basename(e['path'])}: {e['error']}")


# ---------- 主入口 ----------
def _mode_a_result(args):
    """Mode A (own-JSONL) / Mode B 单 transcript: 载入 → 聚合 → (result, topo).
    抽出供 one-shot 渲染 + --watch 循环复用 (DRY, E5 §8.8). 调用方须先保证 transcript adapter 可用."""
    mapping, _generations_raw = load_generations_map()   # Phase 3: 全局 lineage map (一次; Mode B 查 map 恢复, Mode A live 通常 no-op 确认)
    if getattr(args, "transcript", None):
        records, nfiles, skipped = load_transcript(args)
        # 读端补全 (与 run_scan line 660 / live 同核, 单一计费口径): transcript 源的 async_launched 记录
        # 也扫 subagents/agent-*.jsonl 补终态 → capturePhase=complete. 否则 session/root 详情页 asyncCount 把
        # 已跑完的 async spawn 全算"在飞" (cry wolf; fleet 走 scan reconcile 是对的, 详情页漏了这步才发散).
        # proot 从 transcript 路径上溯到 'projects' 根 (镜像 run_scan); 否则默认 ~/.claude/projects 找不到
        # 非标源 (非标 project) agent 终态 → async 全误报在飞. 无 agent 文件/异常 → reconcile swallow 不动.
        records = _reconcile_live_records(records, projects_root=_projects_root(args.transcript))
    else:
        records, nfiles, skipped = load_records(args)
        records = _reconcile_live_records(records)   # 读端补全 (live 专用): async/historical 记录 → agent 文件终态累计 (单一计费口径)
    _apply_generation_map(records, mapping)   # Phase 3: post-reconcile 缝合 (Mode B transcript 查 map; 空 map no-op)
    events = [to_event(r) for r in records]
    sub_events = [e for e in events if e.get("recordType") == "SubagentCall"]
    topo = build_topology(sub_events)

    result = {
        "files": nfiles,
        "modeLabel": "B · transcript" if getattr(args, "transcript", None) else "A · own-JSONL",
        "recordsTotal": len(records),
        "byTrack": dict(Counter(e.get("recordType") for e in events)),
        "sessions": sorted({e.get("sessionId") for e in sub_events if e.get("sessionId")}),
        "skippedBadLines": skipped,
        "grandTotal": grand_total(sub_events),
        "bySubagentType": by_subagent_type(sub_events),
        "callGraph": call_graph(topo),
        "bySkill": by_skill(events),   # §8.11: 须传全部 events (spawned 映射 + SkillCall 都在)
        "consistency": consistency(sub_events, topo),
    }

    # §9 双数据源: live 源也吐 perSession → dashboard 同形渲染 (record.py JSONL 按 sessionId 分组).
    # Event IR (to_event) 不投影 projectName → 从原始 records 查 project; evs 仍走 token/拓扑 聚合.
    _rec_proj = {}   # sessionId -> projectName (取该 session 首条 record; 缺则 "(unknown)")
    for r in records:
        if r.get("recordType") == "SubagentCall" and r.get("sessionId") and r["sessionId"] not in _rec_proj:
            _rec_proj[r["sessionId"]] = r.get("projectName") or "(unknown)"
    _by_sid = defaultdict(list)
    for e in sub_events:
        if e.get("sessionId"):
            _by_sid[e["sessionId"]].append(e)
    # transcript 源: 单文件 root 主线 context 峰值 (对齐 _mode_b §8.3 口径, line 527); live/jsonl 源无 root context 通道 → 0.
    # _by_sid 在 transcript 单文件源里通常只有 root sid (subagent spawn 记在 root 名下); 仍按 sid==root_sid 精确归属, 防御多 sid.
    _root_rcs, _root_sid = None, None
    _root_ctx_errors = {"count": 0, "sample": None}
    if getattr(args, "transcript", None) and root_context_samples:
        _root_sid = os.path.splitext(os.path.basename(args.transcript))[0]
        _root_rcs = root_context_samples(args.transcript)   # 调一次: peak + sum 都取 (§8.3 + §7 计费口径)
        if count_ctx_limit_errors:                    # 同 import 块 (与 root_context_samples 同在); §8.3 💥 爆掉事件
            _root_ctx_errors = count_ctx_limit_errors(args.transcript)
    # §8.6 ✗ tool 失败 per-spawn (并入 callChains=topo) + root 计数 (并入 root sid perSession 行).
    # session 详情页经 _handle_session → run_source("transcript:..") → 本函数, callChains 即本 topo →
    # 花名册行 ✗ 指向元凶 spawn. 按 spawnedAgentId 解析 <sid>/subagents/agent-*.jsonl 建一次 id→path 映射,
    # count_tool_errors 并入 topo 条目; 与 spawn status 分轨 (status=completed 但内部某轮 is_error 仍计), 不并 successRate/grandTotal.
    _root_tool_errors = 0
    if getattr(args, "transcript", None) and count_tool_errors and _root_sid:
        _agent_paths = {}
        for _ap in glob.glob(os.path.join(os.path.dirname(args.transcript), _root_sid, "subagents", "agent-*.jsonl")):
            _agent_paths[os.path.basename(_ap)[len("agent-"):-len(".jsonl")]] = _ap
        for d in topo:
            _ap = _agent_paths.get(d.get("spawnedAgentId"))
            d["toolErrorCount"] = count_tool_errors(_ap)["count"] if _ap else 0
        _root_tool_errors = count_tool_errors(args.transcript)["count"]
    result["perSession"] = [
        _per_session_row(
            project=_rec_proj.get(sid, "(unknown)"),
            sid=sid, spawns=len(evs),
            grand_total_dict=grand_total(evs),
            dur_ms=sum(e.get("durationMs", 0) for e in evs
                       if isinstance(e.get("durationMs"), (int, float))),
            consistent=consistency(evs, build_topology(evs))["consistent"],
            mode_label="A · live",
            ctx_peak=((_root_rcs or {}).get("peak", 0) if sid == _root_sid else 0),
            ctx_limit_errors=(_root_ctx_errors if sid == _root_sid else None),
            root_usage=((_root_rcs or {}).get("sum") if sid == _root_sid else None),
            async_count=sum(1 for e in evs if e.get("status") == "async_launched" and e.get("capturePhase") != "complete"),
            tool_error_count=(_root_tool_errors if sid == _root_sid else 0),
            generation_id=evs[0].get("generationId") or sid,   # Phase 3 (§10.1): evs 已带 map 覆盖后的 generationId; 缺→sid
        )
        for sid, evs in _by_sid.items()
    ]
    result["generations"] = aggregate_generations(result["perSession"], _budget_threshold())   # Phase 3 (§10.1): live logdir 跨 session 同 carrier 时多 session 卷起; 单 session→singleton. budgetState (缺口 1): threshold 配了才挂
    return result, topo


def _watch_files(args):
    """--watch: 返回要 watch mtime 的文件列表 (只 stat, 不读内容).
    --jsonl → 单文件; --transcript → 单文件 (或目录 mtime); 否则 own-logdir <logdir>/*/*.jsonl."""
    if getattr(args, "transcript", None):
        return [args.transcript]
    if getattr(args, "jsonl", None):
        return [args.jsonl]
    logdir = args.logdir or DEFAULT_LOGDIR
    return sorted(glob.glob(os.path.join(logdir, "*", "*.jsonl")))


def _watch_max_mtime(files):
    """files → 存在文件 max mtime (float); 空 / 全不存在 → None."""
    mt = None
    for f in files:
        try:
            m = os.path.getmtime(f)
        except OSError:
            continue
        if mt is None or m > mt:
            mt = m
    return mt


def _watch_loop(args):
    """C 形态 live-tail (§8.8): 循环重算 → ANSI 清屏 → 重印表. mtime 变才重渲 (省 CPU); Ctrl-C 退出.
    纯 print + ANSI clear, 零依赖 (无 rich / curses)."""
    last = None
    try:
        while True:
            cur = _watch_max_mtime(_watch_files(args))
            if cur != last:
                result, topo = _mode_a_result(args)
                print("\033[2J\033[H", end="")              # ANSI clear screen + cursor home (跨平台, 零依赖)
                render(result, topo, args.tree)
                sys.stdout.flush()                          # 非 tty (管道) 时强制刷新, 保证可见
                print(f"\n[live-tail · watching {len(_watch_files(args))} file(s) · 2s 轮询 · Ctrl-C 退出]",
                      flush=True)
                last = cur
            time.sleep(2)
    except KeyboardInterrupt:
        print("\n(stopped)")


def main():
    ap = argparse.ArgumentParser(
        prog="analyze.py",
        description="agent-insight offline reader (Mode A · own-JSONL, §9). 还原 token 账 + 调用拓扑 + 自洽诊断.",
    )
    ap.add_argument("--jsonl", help="单个 JSONL 文件路径 (与 --logdir 互斥)")
    ap.add_argument("--transcript", default=None,
                    help="Mode B: CC session transcript (root '<sid>.jsonl' 或 '<sid>/' 目录). "
                         "解析 toolUseResult → per-subagent token + 拓扑 (§9.2·B). 只重建 depth-2 "
                         "(CC transcript 不持久化嵌套结构化 spawn, §9.3 caveat).")
    ap.add_argument("--logdir", default=None, help=f"JSONL 根目录 (默认 {DEFAULT_LOGDIR})")
    ap.add_argument("--project", default=None, help="project 子目录名 (不给则扫 logdir 下全部; "
                    "--scan-projects 下则作 scan 内 project 过滤)")
    ap.add_argument("--since", default=None, help="只读 >= 该日期 (YYYY-MM-DD) 的文件")
    ap.add_argument("--scan-projects", nargs="?", const="__default__", default=None,
                    help="Mode B · fleet: 扫 CC projects 目录 (裸 flag = ~/.claude/projects, "
                         "带值 = 该路径), 逐 session 跑 Mode B 管线后跨 session 聚合 "
                         "(per-session 汇总 + 跨 session 合计 + top outlier + scan 自洽). "
                         "与 --transcript/--jsonl/--logdir 互斥; 可配 --project/--json.")
    ap.add_argument("--tree", action="store_true", help="打印逐条调用链")
    ap.add_argument("--json", action="store_true", help="机器可读 JSON 输出 (测试 / 下游 / §8 dashboard)")
    ap.add_argument("--watch", action="store_true",
                    help="C 形态 live-tail (§8.8): 循环重算 + 清屏重印表 (2s 一次, mtime 变才重渲). "
                         "与 --json 共存 (--watch 优先, 进交互循环). Ctrl-C 退出.")
    args = ap.parse_args()

    # Mode B · scan-projects 分支 (fleet-wide): 与单文件/单 transcript/own-logdir 模式互斥.
    if getattr(args, "scan_projects", None) is not None:
        if getattr(args, "transcript", None) or getattr(args, "jsonl", None) or getattr(args, "logdir", None):
            print("Error: --scan-projects 与 --transcript/--jsonl/--logdir 互斥.", file=sys.stderr)
            return 1
        if discover_root_transcripts is None or load_transcript is None:
            print("Error: --scan-projects 需要 transcript_adapter.py (Mode B).", file=sys.stderr)
            return 1
        scan_dir = args.scan_projects if args.scan_projects != "__default__" else DEFAULT_PROJECTS
        per_session, errors = run_scan(args)
        result = aggregate_scan(per_session, errors, scan_dir, getattr(args, "project", None))
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        render_scan(result)
        return 0

    if getattr(args, "transcript", None) and load_transcript is None:
        print("Error: --transcript 需要 transcript_adapter.py (Mode B).", file=sys.stderr)
        return 1
    result, topo = _mode_a_result(args)

    if args.watch:               # C 形态 live-tail (§8.8): 进交互循环 (覆盖 --json, 永不吐 JSON)
        _watch_loop(args)
        return 0

    if args.json:
        result["callChains"] = topo
        if root_context_samples:   # Plan 3a: 单 session --json 附 root 主线逐 turn context (曲线数据源)
            result["rootContext"] = root_context_samples(args.transcript)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    render(result, topo, args.tree)
    return 0


if __name__ == "__main__":
    sys.exit(main())
