"""Mode B transcript ingest adapter (§9.2·B · OfflineAdapter).

把 CC session transcript 解析成 §6 形状 record, 交回 analyze.py 的 to_event 缝 —— 之后整条管线
(build_topology / grand_total / by_subagent_type / call_graph / consistency / CLI) 复用不动, 零改动.

设计铁律: 只换 ingest 入口. F9 铁律 —— 只读顶层 `toolUseResult` 结构化字段, 绝不 parse
`tool_result.content` 的伪 XML.

caller = 文件归属 (§9.2): root 文件 → caller.agentId=null / isRoot=true;
         <sid>/subagents/agent-<X>.jsonl → caller.agentId=X / isRoot=false.
subagents/ 扁平 (无目录嵌套), 扫描所有文件, depth 由 build_topology 自然涌现 —— 逻辑 depth-general.

平台边界 (§9.3, 多个真实编排 session 样本可推广):
  CC transcript 只对 root 直发 Agent 调用持久化结构化 `toolUseResult` (带 usage/totalTokens/agentType).
  嵌套 Agent 调用 (子 agent 再派子 agent) 只落成 message content 的 tool_use/tool_result 块,
  该行 toolUseResult=null → Mode B 只重建 depth-2 (root→agent). depth-3+ 的 token/拓扑须靠 live hook.
  §9.3#4 逐值交叉验证: 对 depth-2 成立 (实测 5/5 精确相等), 对 depth-3 不成立 (transcript 缺失).

契约同 analyze.load_records: load_transcript(args) → (records, nfiles, skipped).
"""
import glob
import json
import os
import re
import sys

# 同目录 import 保险 (本模块可能被 analyze.py / server.py / hooks/ 以不同 cwd/sys.path 调用).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from terminal_stats import terminal_stats   # 单一计费口径源 (offline + live 共用, 见 terminal_stats.py)


def _warn(msg):
    print(f"[transcript_adapter] {msg}", file=sys.stderr)


def _derive_session_id(path):
    """从 '<dir>/<sid>.jsonl' 取 <sid>; 非 .jsonl → None."""
    base = os.path.basename(path)
    if base.endswith(".jsonl"):
        return base[:-len(".jsonl")]
    return None


def _read_first_session_id(path):
    """fallback: 从首条 dict 行读 sessionId 字段."""
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                if isinstance(obj, dict) and obj.get("sessionId"):
                    return obj["sessionId"]
    except Exception:
        pass
    return None


def _token_or_none(usage, key):
    """镜像 record.py:140-146 透传语义: 非整数 → None (保持与 recorder 一致, 聚合时 _tok 当 0)."""
    v = (usage or {}).get(key)
    return v if isinstance(v, int) else None


def _resolve_model_fallback(spawned_id, tooluse_model, agent_file_map):
    """resolvedModel = toolUseResult.resolvedModel → agent-<spawned>.jsonl 首条 assistant message.model → null.
    best-effort (F6), never raises. 1d2b2004 实测: 36 条 root spawn 中 24 条缺 resolvedModel,
    回退 agent 文件 message.model 命中 (如 glm-5.2)."""
    if tooluse_model:
        return tooluse_model
    path = agent_file_map.get(spawned_id)
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                if not isinstance(obj, dict):
                    continue
                msg = obj.get("message")
                if isinstance(msg, dict) and msg.get("model"):
                    return msg["model"]
    except Exception:
        pass
    return None


def _agent_file_stats(spawned_id, agent_file_map):
    """读 agent-<spawned>.jsonl → (model, usage). 委托 terminal_stats (单一计费口径源, 见该模块 docstring).

    token 权威源 = agent 文件终态累计: root toolUseResult.usage 只携末轮 / async 恒 None (见 terminal_stats).
    bulletproof: 无文件/异常 → (None, None)."""
    path = agent_file_map.get(spawned_id) if agent_file_map else None
    if not path or not os.path.isfile(path):
        return None, None
    return terminal_stats(path)


def _agent_meta(spawned_id, agent_file_map):
    """读 agent-<spawned>.meta.json sidecar → {agentType, name, description, toolUseId} or None.
    async spawn (status=async_launched) 的 root tool_result 缺 agentType (→ 误显 'unknown'); sidecar 补
    (F6 best-effort). 实证 sidecar 形态: {agentType, description, name, toolUseId}.
    bulletproof: 无/异常 → None."""
    path = agent_file_map.get(spawned_id) if agent_file_map else None
    if not path:
        return None
    meta_path = os.path.splitext(path)[0] + ".meta.json"
    if not os.path.isfile(meta_path):
        return None
    try:
        with open(meta_path) as f:
            m = json.load(f)
        if not isinstance(m, dict):
            return None
        return {k: m.get(k) for k in ("agentType", "name", "description", "toolUseId")}
    except Exception:
        return None


def _build_record(tur, caller_agent_id, caller_is_root, session_id, project_name,
                  agent_file_map, raw_line, caller_turn=None):
    """toolUseResult dict → §6 SubagentCallRecord (镜像 hooks/record.py:128-155, 数据源换成 transcript).

    token 权威源 = agent 自己 transcript 终态累计 (2026-06-19 定调, 见 _agent_file_stats): root
    toolUseResult.usage 只携末轮 / async 恒 None; 有 agent 文件且计费>0 则覆盖, 否则回退 root.
    agentType: async 的 root tool_result 缺 → 从 .meta.json sidecar 补 (_agent_meta), 避免误显 'unknown'.
    caller_turn = 调用方 transcript 里启动该 spawn (Agent tool_use) 的 message 序号 (A2, 拓扑锚点);
        callerAgentId 决定归 root 还是父 spawn (嵌套 depth-2/depth-3); None=反查失败."""
    spawned_id = tur.get("agentId")
    status = tur.get("status")
    meta = _agent_meta(spawned_id, agent_file_map)
    spawned_type = tur.get("agentType") or (meta or {}).get("agentType") or "unknown"

    # token 权威源: agent 文件终态累计 > root tool_result (见 _agent_file_stats). 单遍同时取 model.
    af_model, af_sum = _agent_file_stats(spawned_id, agent_file_map)
    if af_sum and (af_sum["cacheRead"] + af_sum["cacheCreation"] + af_sum["input"]) > 0:
        af_total = (af_sum["input"] + af_sum["output"] + af_sum["cacheCreation"] + af_sum["cacheRead"]) or None
        tokens = {
            "input": af_sum["input"] or None,
            "output": af_sum["output"] or None,
            "cacheCreation": af_sum["cacheCreation"] or None,
            "cacheRead": af_sum["cacheRead"] or None,
            "total": af_total,
        }
    else:
        usage = tur.get("usage") or {}
        parts = [_token_or_none(usage, k) or 0 for k in
                 ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")]
        tokens = {
            "input": _token_or_none(usage, "input_tokens"),
            "output": _token_or_none(usage, "output_tokens"),
            "cacheCreation": _token_or_none(usage, "cache_creation_input_tokens"),
            "cacheRead": _token_or_none(usage, "cache_read_input_tokens"),
            "total": sum(parts) or None,   # 全 0/None → None (避免误报 total=0)
        }

    success = (status == "completed")
    return {
        "schemaVersion": 1,
        "timestamp": raw_line.get("timestamp"),
        "runId": session_id,                    # transcript 无 runId → = sessionId (§9.3#6)
        "generationId": session_id,             # 无 carrier → = sessionId
        "carrierSource": None,                  # transcript 无 carrier
        "projectName": project_name,
        "sessionId": session_id,
        "toolUseId": (meta or {}).get("toolUseId"),   # .meta.json sidecar (root toolUseResult 不携带)
        "caller": {
            "agentId": caller_agent_id,         # None = root 文件归属
            "agentType": None,                  # 此层不带 caller 类型; build_topology 由父事件 subagentType 派生 parentType
            "isRoot": caller_is_root,
        },
        "callerTurn": caller_turn,              # A2: 调用方启动该 spawn 的 message 序号 (拓扑锚点; depth-2=root / depth-3=父 spawn); None=未绑定
        "budgetState": None,
        "recordType": "SubagentCall",
        "subagentType": spawned_type,
        "spawned": {"agentId": spawned_id, "agentType": tur.get("agentType") or (meta or {}).get("agentType")},
        "tokens": tokens,
        "durationMs": tur.get("totalDurationMs"),
        "resolvedModel": tur.get("resolvedModel") or af_model,   # 单遍: agent 文件首条 model (替代 _resolve_model_fallback 双读)
        "success": success,
        "status": status,                       # 原始 status (completed/async_launched/...) — 前端健康信号区分异步后台
        "error": None if success else status,
    }


def _build_skill_record(skill_name, caller_turn, caller_agent_id, caller_is_root,
                        session_id, project_name, ts, success):
    """assistant tool_use(Skill) → §6 SkillCallRecord (§8.11/§9.3#7; D4 单源 tool_use 采集).

    skillName 取自 tool_use.input.skill (root+spawn 同构 —— spawn transcript 不写顶层 toolUseResult,
    故弃旧 '顶层 commandName' 源; 实测 root input.skill 与 commandName 5/5 一致, 零回归). success: root
    从配对顶层 tur.success 反查 (bool); spawn 无 tur → None 诚实缺省. tokens 恒 None (F3: Skill 无 token
    边界, 只追踪 '加载了哪些能力', 不归因成本). caller = 文件归属 (root→isRoot=true; agent-<X>→isRoot=false).
    callerTurn = 含该 tool_use 的 message 序号 (A2, 与 traces/raw 同空间, drillTurn 锚点)."""
    return {
        "schemaVersion": 1,
        "timestamp": ts,
        "runId": session_id,
        "generationId": session_id,
        "carrierSource": None,
        "projectName": project_name,
        "sessionId": session_id,
        "toolUseId": None,
        "caller": {
            "agentId": caller_agent_id,
            "agentType": None,                  # 此层不带 caller 类型 (同 _build_record); by_skill 内部用 spawned 映射补
            "isRoot": caller_is_root,
        },
        "callerTurn": caller_turn,              # A2: 含该 Skill tool_use 的 message 序号 (drillTurn 锚点); None=未绑定
        "budgetState": None,
        "recordType": "SkillCall",
        "skillName": skill_name or "unknown",
        "success": success,                     # None (spawn 无 tur) / bool (root 反查) — 诚实缺省
        "tokens": None,   # 零 token (F3): 不入 grandTotal、不进 token 排名
    }


def parse_transcript_file(path, caller_agent_id, caller_is_root, session_id,
                          project_name, agent_file_map):
    """解析一个 transcript 文件 → §6 records. 流式逐行 (不 full-load, 适配 11310 行大文件).

    A2 turn 语义 (D2.4): turn_idx = 一条 assistant message 的序号. CC 把一条 message 拆多行 jsonl
    (thinking/text/每个 tool_use 各一行, 共享 message.id) → 同 message.id 多行不重复 +1 (镜像
    _collect_turns_by_message 的去重规则: 有 id 行首现 +1/复现不 +1, 无 id 行各 +1).

    Skill 采集 (D4 单源 tool_use, root+spawn 同构): 从 assistant 行 tool_use blocks 采 (name=="Skill"
    → skillName=input.skill|command); spawn transcript 不写顶层 toolUseResult, 故弃旧 '顶层 commandName' 源
    (实测 root input.skill 与 commandName 5/5 一致, 零回归). success 单遍延迟 emit: 收 tool_use 缓存进
    pending_skills, 收配对顶层 tur (user 行, 带 success) 后反查 tool_result.tool_use_id emit (root, 真
    success); 文件结束 flush 未配对的 (spawn 无 tur → success=None 诚实缺省), 保流式大文件契约.

    Agent(SubagentCall) 走顶层 tur.agentId 不动 (root token 真值); depth-3 spawn Agent 不采 (守单一计费核).

    F9: 只读结构化字段 (顶层 toolUseResult dict / tool_use input.skill 字段访问), 绝不 parse content 伪 XML.

    skipped = 仅坏行 (空/非法 JSON/非 dict), 与 analyze.load_records 同口径 (原 '每无 tur 行计 skipped'
    过计 bug 修正 → skippedBadLines 名实相符). 返回 (records, skipped)."""
    records, skipped = [], 0
    turn_idx = -1              # A2 message 序号 (与 _collect_turns_by_message/agent_turn_* 同空间)
    mid_to_turn = {}           # message.id → turn_idx (A2: 同 mid 多行复用, 不重复 +1)
    pending_skills = {}        # tool_use_id → {skill, turn, ts} (D4: 待配对 success 的 Skill tool_use)
    tu_turn = {}               # tool_use_id → message 序号 (A2: 任意 tool_use; SubagentCall 反查 Agent tool_use 得 callerTurn)
    if not path or not os.path.isfile(path):
        return records, skipped
    try:
        f = open(path)
    except Exception:
        return records, skipped
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                skipped += 1
                continue
            if not isinstance(obj, dict):
                skipped += 1
                continue
            # A2 + D4: assistant 行 — 维护 message 序号 + 采 Skill tool_use (单源, root+spawn 同构).
            if obj.get("type") == "assistant" and isinstance(obj.get("message"), dict):
                msg = obj["message"]
                mid = msg.get("id")
                if mid is None:
                    turn_idx += 1                          # 无 id 行各成一条 message
                    cur_turn = turn_idx
                elif mid in mid_to_turn:
                    cur_turn = mid_to_turn[mid]            # 同 mid 复用 (CC 拆多行)
                else:
                    turn_idx += 1
                    mid_to_turn[mid] = turn_idx
                    cur_turn = turn_idx
                _c = msg.get("content")
                if isinstance(_c, list):
                    for _b in _c:
                        if (isinstance(_b, dict) and _b.get("type") == "tool_use"
                                and _b.get("id")):
                            tu_turn[_b["id"]] = cur_turn       # A2: 任意 tool_use → message 序号 (SubagentCall 反查 callerTurn)
                            if _b.get("name") == "Skill":
                                _inp = _b.get("input") if isinstance(_b.get("input"), dict) else {}
                                _skill = _inp.get("skill") or _inp.get("command")
                                pending_skills[_b["id"]] = {
                                    "skill": _skill, "turn": cur_turn, "ts": obj.get("timestamp"),
                                }
                # 不 continue: 合成 fixture (及潜在边缘) 可能把 toolUseResult 放在 assistant 行
                # (real CC 实测 tur 仅在 user 行, 但不假设). 无 tur 的 assistant 行 ↓ isinstance 检查后 continue.
            tur = obj.get("toolUseResult")
            if not isinstance(tur, dict):
                continue                                # user-text 行等: 合法但无 record, 非 bad line
            # F9 结构化分流: 带 agentId → SubagentCall (root 真 spawn, 带 usage/agentType, 不动);
            # 无 agentId → 反查 pending Skills (root Skill result; spawn 无 tur → 不进此支, EOF flush).
            if tur.get("agentId"):
                # callerTurn: 本 user 行 tool_result 的 tool_use_id → 反查 tu_turn (Agent tool_use 所在
                # message 序号 = 调用方启动该 spawn 的 turn). 同一 assistant message 的多 tool_result 共享
                # 序号 → 首个命中即正确. None=反查失败 (拓扑锚点不渲染).
                _ct = None
                _cmsg = obj.get("message")
                if isinstance(_cmsg, dict) and isinstance(_cmsg.get("content"), list):
                    for _b in _cmsg["content"]:
                        if isinstance(_b, dict) and _b.get("type") == "tool_result":
                            _tuid = _b.get("tool_use_id")
                            if _tuid and _tuid in tu_turn:
                                _ct = tu_turn[_tuid]
                                break
                records.append(_build_record(tur, caller_agent_id, caller_is_root,
                                             session_id, project_name, agent_file_map, obj, _ct))
                continue
            _emitted = []
            _cmsg = obj.get("message")
            if isinstance(_cmsg, dict) and isinstance(_cmsg.get("content"), list):
                for _b in _cmsg["content"]:
                    if isinstance(_b, dict) and _b.get("type") == "tool_result":
                        _tuid = _b.get("tool_use_id")
                        if _tuid and _tuid in pending_skills:
                            _p = pending_skills[_tuid]
                            records.append(_build_skill_record(
                                _p["skill"], _p["turn"], caller_agent_id, caller_is_root,
                                session_id, project_name, _p["ts"], bool(tur.get("success"))))
                            _emitted.append(_tuid)
            for _id in _emitted:
                pending_skills.pop(_id, None)
        # EOF flush: 未配对的 Skill tool_use (spawn 无顶层 tur → success=None 诚实缺省)
        for _p in pending_skills.values():
            records.append(_build_skill_record(
                _p["skill"], _p["turn"], caller_agent_id, caller_is_root,
                session_id, project_name, _p["ts"], None))
    return records, skipped


def discover_root_transcripts(scan_dir, project=None):
    """扫 CC projects 目录, 返回 sorted root '<sid>.jsonl' 路径列表 (pure discovery, 零 parse).

    形态自适应 (support both validation modes):
      - scan_dir 直接含 UUID-stem *.jsonl → 它就是单个 project 子目录 (如 --scan-projects <projects>/<proj>);
      - 否则 scan_dir 是 projects 父目录 → 遍历 <proj>/ (project 给定则只该 proj).

    UUID 形过滤 (^[0-9a-f]{8}-[0-9a-f]{4}-...) 自动排除 memory/ 子目录、<sid>/subagents/ 内 agent-*.jsonl、
    *.meta.* 文件 —— 只收 root session transcript. discovery 不预判有无 <sid>/subagents/, 零 Agent spawn 的
    session 仍被发现、由 load_transcript 优雅降级 (depth-2 from root)."""
    uuid_re = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    if not scan_dir or not os.path.isdir(scan_dir):
        return []

    def _uuid_jsonls(d):
        out = []
        try:
            names = sorted(os.listdir(d))
        except OSError:
            return out
        for fname in names:
            if fname.endswith(".jsonl") and uuid_re.match(fname[:-len(".jsonl")]):
                out.append(os.path.join(d, fname))
        return out

    # 形态 1: scan_dir 直接含 UUID jsonl → 它本身就是 project 目录.
    direct = _uuid_jsonls(scan_dir)
    if direct:
        return direct
    # 形态 2: scan_dir 是 projects 父目录 → 遍历 <proj>/.
    if project:
        return _uuid_jsonls(os.path.join(scan_dir, project))
    paths = []
    for d in sorted(os.listdir(scan_dir)):
        sub = os.path.join(scan_dir, d)
        if os.path.isdir(sub):
            paths.extend(_uuid_jsonls(sub))
    return paths


def root_context_samples(path, limit=200000):
    """读 root transcript → 逐 turn root 主线 context 序列 (§8.3 per-turn context 数据层; Plan 3a).

    与 parse_transcript_file 物理分离的独立通道 (读 root 主线 turn, 不产 SubagentCall/SkillCall
    record, 不入 grand_total/consistency 管线) —— §9.3#4: root per-turn usage 是独立数据通道
    (实测 100% root assistant turn 携带 usage). A2 后复用 _collect_turns_by_message 取 turn 序列
    (该 helper 读结构化 content blocks 做 message 合并, F9 兼容; 本层只消费 usage 四桶, 不 parse 伪 XML).

    per-turn context = input + cacheCreation + cacheRead (§8.3; output 不进 prompt 侧).
    session peak = max over turns (§8.3).
    sum 三桶 = root 主线逐 message 真实计费累加 (§7 计费口径 cache 命中率; 2026-06-19 定调:
    按计费规则, 不被重复算钱的 token 全算上 — 各 turn cacheRead 是独立真实计费事件, 累加非重复;
    与 subagent grand_total 同公式, 统一一把尺子, 取代纯 root session 显 — 的旧逻辑).
    去重 (A2): 同 message 多行按 message.id 去重留终态行 (stop_reason 优先) 由 _collect_turns_by_message
    统一处理; 逐行求和会把 input 算 N 遍 (实测虚胖 50×). cr/cc 不受影响.

    bulletproof: 任何异常 → 返 {samples:[], peak:0, peakTurn:None, sum:{0,0,0}} (绝不拖垮 scan; per-session 隔离).
    limit 仅透传 (透明阈值, 供 renderer 做 ⚠ glyph 判定, §8.3); 数据层本身不下 glyph 结论."""
    empty = {"samples": [], "peak": 0, "peakTurn": None, "limit": limit,
             "sum": {"input": 0, "cacheCreation": 0, "cacheRead": 0}}
    try:
        if not path or not os.path.isfile(path):
            return empty
        # A2: 复用 _collect_turns_by_message (DRY) —— turn = 一条 message, 序号 i 与 agent_turn_traces/
        # agent_turn_raw 同空间. 无 usage 的 message 不进 sample (ctx 数据层) 但序号空间一致: turnIndex=i
        # 锁死全 message 空间 (修历史隐性偏移: 旧 row_idx 按 assistant 行计, 与 dedup 后的 i 错位).
        samples = []
        for m in _collect_turns_by_message(path):
            ud = m["usage"]
            if not isinstance(ud, dict):
                continue
            inp = ud.get("input_tokens") or 0
            cc = ud.get("cache_creation_input_tokens") or 0
            cr = ud.get("cache_read_input_tokens") or 0
            samples.append({"ts": m["ts"], "input": inp, "cacheCreation": cc, "cacheRead": cr,
                            "ctx": inp + cc + cr, "turnIndex": m["i"], "i": m["i"]})
        if not samples:
            return empty
    except Exception:
        return empty
    peak, peak_turn = 0, None
    s_in = s_cc = s_cr = 0
    for s in samples:
        if s["ctx"] > peak:
            peak, peak_turn = s["ctx"], s["i"]
        s_in += s["input"]; s_cc += s["cacheCreation"]; s_cr += s["cacheRead"]
    return {"samples": samples, "peak": peak, "peakTurn": peak_turn, "limit": limit,
            "sum": {"input": s_in, "cacheCreation": s_cc, "cacheRead": s_cr}}


def count_ctx_limit_errors(path):
    """读 root transcript → context window limit 爆掉事件 (§8.3 💥 状态 glyph 数据层; 2026-06-19 实证锁定信号).

    真信号: type=='assistant' 行的 message.content **顶层 text 块**, strip 后以 'API Error' 起头
    且含 'context window limit' (实测真值 'API Error: The model has reached its context window limit.').
    **严格限定 assistant 顶层 text** —— 防 echo 假阳性: 'context window limit' 这串在 user/system 行、
    或 tool result 内联旧 transcript 错误文本里也会出现 (实测 9aa81da2 grep 该串=4 但真爆=1, 00cab3c5 grep=1
    但真爆=0 —— 多余的全是 echo; 00cab3c5 那个 1 是它 Read 旧 jsonl 续接时把旧错误文本 echo 进 user 行).

    与 root_context_samples 不同: 本函数**读 content** (爆掉信号只在 content text 里, usage 桶里没有),
    但只取顶层 text 块整段起头判定, 不 parse 伪 XML (F9 兼容). 爆掉 turn 常无 usage → 不能并入 root_context_samples
    (后者显式 content-safe '绝不读 message.content' 且要求 usage dict, §9.3#4), 故独立通道.

    bulletproof: 任何异常 → {"count": 0, "sample": None} (per-session 隔离, 绝不拖垮 scan).
    返 {"count": N, "sample": 首个错误文本 or None}."""
    empty = {"count": 0, "sample": None}
    if not path or not os.path.isfile(path):
        return empty
    count = 0
    sample = None
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or "context window limit" not in line:   # 廉价预筛 (跳过绝大多数行)
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if not isinstance(obj, dict) or obj.get("type") != "assistant":
                    continue
                msg = obj.get("message")
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "text":
                        continue
                    tx = (block.get("text") or "").strip()
                    if tx.startswith("API Error") and "context window limit" in tx:
                        count += 1
                        if sample is None:
                            sample = tx
                        break   # 一个 assistant turn 只计一次 (防同 turn 多块重复)
    except Exception:
        return empty
    return {"count": count, "sample": sample}


def count_tool_errors(path):
    """读 transcript → tool_result is_error 失败计数 (§8.6 ✗ tool 失败定位数据层; 2026-06-21).

    真信号: type=='user' 行 message.content 的 tool_result block, 结构化字段 is_error==true (F9 合规,
    非 content 伪 XML). tool 名靠单遍累积映射 tool_use_id→name (assistant tool_use block 先于其
    user tool_result, 故累积 pass 即可反查失败 tool 名).

    与 count_ctx_limit_errors (ctx 爆掉, assistant 顶层 'API Error ... context window limit') 分轨:
    本函数读 user 行结构化 is_error — provider/CC 层单 tool 执行失败 (Bash 非零退出 / Edit 未命中 / Read 拒绝).

    **与 spawn 成功率 (SubagentCall status) 分轨**: is_error ≠ status. 一个 spawn 可 status=completed 但
    内部某轮 Bash is_error → 独立 ✗ 信号, 绝不并 successRate/grandTotal (红线: 单一计费核).

    bulletproof: 任何异常 → {"count": 0, "sample": None} (per-session 隔离, 绝不拖垮 scan).
    返 {"count": N, "sample": 首个失败 tool 名 or tool_use_id or None}."""
    empty = {"count": 0, "sample": None}
    if not path or not os.path.isfile(path):
        return empty
    count = 0
    sample = None
    name_by_tid = {}   # tool_use_id → name (assistant 行 tool_use 先于 user 行 tool_result, 单遍累积)
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
                typ = obj.get("type")
                msg = obj.get("message")
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                if typ == "assistant":
                    for blk in content:
                        if isinstance(blk, dict) and blk.get("type") == "tool_use" and blk.get("id"):
                            name_by_tid[blk["id"]] = blk.get("name")
                elif typ == "user":
                    for blk in content:
                        if not isinstance(blk, dict) or blk.get("type") != "tool_result":
                            continue
                        if not blk.get("is_error"):   # 结构化字段 (F9), 非 content 伪 XML
                            continue
                        count += 1
                        if sample is None:
                            tid = blk.get("tool_use_id")
                            sample = name_by_tid.get(tid) or tid or "(tool)"
    except Exception:
        return empty
    return {"count": count, "sample": sample}


def _short_target(inp):
    """tool_use.input → 单标签 target (≤60 字符): file_path / command 首行 / pattern / query.
    仅短摘要 (§8.6); 全文 input 是 turn 原文. spawn 详情 on-demand per-spawn summary, 非 bulk 聚合 (F9 兼容)."""
    if not isinstance(inp, dict):
        return None
    for k in ("file_path", "path", "notebook_path", "fileName", "filePath", "skill", "subagent_type"):
        v = inp.get(k)
        if isinstance(v, str) and v:
            return v[:60]   # subagent_type = Agent/Task 调用的 agent 名 (如 'Explore'); 用户报: root Agent turn 不显哪个 agent
    v = inp.get("command")
    if isinstance(v, str) and v:
        return (v.splitlines()[0] if v.splitlines() else v)[:60]
    for k in ("pattern", "query", "regexp", "search_string", "url"):
        v = inp.get(k)
        if isinstance(v, str) and v:
            return v[:60]
    return None


def _summarize_tools(blocks):
    """同 message 多 tool_use (A2 turn=message) → 合并 tag: 去重保序, 重复显 ×N.
    返回 (tool_str, first_target). tool_str 例 'Bash · Skill' / 'Bash ×2 · Read' / 'Read';
    first_target = 首个 tool_use 的 _short_target (单标签, ≤60). 仅 §8.6 spawn 详情 摘要, 非全文 (F9 兼容).

    旧实现遍历 blocks 取首个 tool_use name 即 break → 一 message 含 bash+skill 时 skill 漏显 (用户报)."""
    order = []
    counts = {}
    first_target = None
    for blk in blocks:
        if not (isinstance(blk, dict) and blk.get("type") == "tool_use"):
            continue
        name = blk.get("name")
        if not name:
            continue
        if name not in counts:
            counts[name] = 0
            order.append(name)
        counts[name] += 1
        if first_target is None:
            first_target = _short_target(blk.get("input"))
    if not order:
        return None, None
    parts = [f"{n} ×{counts[n]}" if counts[n] > 1 else n for n in order]
    return " · ".join(parts), first_target


def _content_char_count(content):
    """tool_result.content → 字符数 (计数, 非文本; §8.6 代理用). content 可为 str 或 [{type:text,text}]."""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        n = 0
        for blk in content:
            if isinstance(blk, dict):
                t = blk.get("text") or blk.get("content")
                if isinstance(t, str):
                    n += len(t)
        return n
    return 0


def _collect_turns_by_message(path):
    """单遍读 transcript → 按 message.id 去重的有序 message 列表 (A2 'turn=一条 message' 语义的单一真值源).

    CC 把一条 assistant message 按内容块 (thinking/text/各 tool_use) 拆成多行 jsonl, 共享同一 message.id;
    按 message.id 去重 (无 id 行各自独立) 后, 序号 i 即 '真实 turn' 序号 (一条 message 一个 turn). 供 4 处共用
    (agent_turn_traces / agent_turn_raw / root_context_samples / parse_transcript_file 的 turn_idx), 防口径漂移 (D1).

    谓词: 只 type=='assistant' 且 message 是 dict (与历史 agent_turn_traces/root_context_samples 一致).
    dedup 赢家 (同 message 取哪行代表 ts/stop_reason/usage): (1 if stop_reason else 0, row_seq) max ——
    stop_reason 优先 + 后行优先 (镜像历史 root_context_samples dedup + terminal_stats). 实证: 同 message 多行
    75% usage 全同、25% 是 'placeholder(0,0,0,0) vs real terminal' 二选一, 0 组两非零打架 → 赢家恒带真终态 usage.

    返回 messages: [{i, mid, ts, stop_reason, usage, blocks[text+tool_use 跨行合并], tool_use_ids}].
    bulletproof: 无文件/异常 → []."""
    messages = []
    if not path or not os.path.isfile(path):
        return messages
    by_key = {}    # key(mid 或 _noid_seq) → 累积 dict
    order = []     # key 插入序 (保时序)
    try:
        with open(path) as f:
            noid_seq = 0
            row_seq = 0
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if not isinstance(obj, dict) or obj.get("type") != "assistant":
                    continue
                msg = obj.get("message")
                if not isinstance(msg, dict):
                    continue
                mid = msg.get("id")
                if mid is None:
                    key = "_noid_" + str(noid_seq)
                    noid_seq += 1
                else:
                    key = mid
                row_seq += 1
                cur = by_key.get(key)
                if cur is None:
                    cur = {"mid": mid, "ts": obj.get("timestamp"),
                           "stop_reason": msg.get("stop_reason"),
                           "usage": msg.get("usage") if isinstance(msg.get("usage"), dict) else None,
                           "blocks": [], "tool_use_ids": [],
                           "_prio": (1 if msg.get("stop_reason") is not None else 0, row_seq)}
                    by_key[key] = cur
                    order.append(key)
                content = msg.get("content")
                if isinstance(content, list):
                    for blk in content:
                        if not isinstance(blk, dict):
                            continue
                        bt = blk.get("type")
                        if bt in ("text", "tool_use"):
                            cur["blocks"].append(blk)
                            if bt == "tool_use" and blk.get("id"):
                                cur["tool_use_ids"].append(blk["id"])
                prio = (1 if msg.get("stop_reason") is not None else 0, row_seq)
                if prio > cur["_prio"]:
                    cur["_prio"] = prio
                    cur["ts"] = obj.get("timestamp")
                    cur["stop_reason"] = msg.get("stop_reason")
                    cur["usage"] = msg.get("usage") if isinstance(msg.get("usage"), dict) else None
    except Exception:
        return []
    for i, key in enumerate(order):
        cur = by_key[key]
        cur["i"] = i
        del cur["_prio"]
        messages.append(cur)
    return messages


def agent_turn_traces(path, limit=200000):
    """读 agent-<id>.jsonl → 逐 turn traces (§8.6 Level 3 主体, Plan C).

    per-turn token 混合显示 (§8.6 边界2): assistant usage 非零 → 真 token (input/cc/cr/out);
    记 0 (provider artifact, §9.3#4) → 回退该 turn 触发的 tool_result 字符数代理
    (字符≠token, 代码≈0.25 tok/char, 只作相对大小).

    content-safe summary (spawn 详情 on-demand per-spawn, 非 bulk 聚合): 读 usage / stop_reason /
    tool name + 单标签 target (file_path/command 首行/pattern/query, ≤60 字符) /
    result 字符数 (计数, 非文本). 全文 input/result/text 是 turn 原文 (agent_turn_raw).

    outlier (§8.6 '最大的几行 ⚠ 高亮'): burden > 1.5×mean → ⚠ (诚实: 均匀 spawn 无标记,
    非恒 top-N). burden = 本 turn 新进上下文 (input+cc; 剔除 cacheRead=重读已缓存, 非本 turn 增量 —
    旧式含 cacheRead ≈ 累积上下文, 单调致 mean 被抬、⚠ 几乎不触发) 或 resultChars (0-turn 代理).

    bulletproof: 异常 → {turns:[], n:0, limit} (绝不拖垮; per-spawn 隔离).
    limit 仅透传 (与 root_context_samples 一致)."""
    empty = {"turns": [], "n": 0, "limit": limit}
    try:
        if not path or not os.path.isfile(path):
            return empty
        # A2: turn = 一条 assistant message (dedup by message.id). 序号 i 与 agent_turn_raw / root_context_samples 同空间.
        messages = _collect_turns_by_message(path)
        if not messages:
            return empty
        # result_chars: 扫 user tool_result 行 (tool_use_id → 字符数; §8.6 0-turn 代理) + is_error (§8.6 ✗ tool 失败)
        result_chars = {}
        result_errors = {}   # tool_use_id → bool(is_error) — 结构化字段 (F9), 供 turn 行标 ✗
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if not isinstance(obj, dict) or obj.get("type") != "user":
                    continue
                msg = obj.get("message")
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                for blk in content:
                    if isinstance(blk, dict) and blk.get("type") == "tool_result":
                        tid = blk.get("tool_use_id")
                        if tid:
                            result_chars[tid] = _content_char_count(blk.get("content"))
                            result_errors[tid] = bool(blk.get("is_error"))
        turns = []
        for m in messages:
            ud = m["usage"]
            inp = _token_or_none(ud, "input_tokens") or 0
            cc = _token_or_none(ud, "cache_creation_input_tokens") or 0
            cr = _token_or_none(ud, "cache_read_input_tokens") or 0
            out = _token_or_none(ud, "output_tokens") or 0
            real = (inp + cc + cr + out) > 0
            tu_ids = list(m["tool_use_ids"])
            tool, target = _summarize_tools(m["blocks"])   # A2: 一 message 多 tool_use → 合并 tag (去重保序, 重复 ×N); 旧 break 取首个 → bash+skill 漏 skill
            # 逐 tool_use 列表 (spawn 详情 turn 行 per-tool chip, 各自带 target): 不去重, 一调用一行 (Bash×2 显两行各自 command)
            tools_list = [{"name": b.get("name"), "target": _short_target(b.get("input"))}
                          for b in m["blocks"]
                          if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name")]
            # §8.6 ✗ tool 失败: 本 turn 内 is_error 的 tool_use → tool 名 (反查 result_errors; 供 turn 行标 ✗ + tooltip)
            tid2name = {b.get("id"): b.get("name") for b in m["blocks"]
                        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id")}
            tool_errors = [tid2name.get(tid) or tid for tid in tu_ids if result_errors.get(tid)]
            turns.append({
                "i": m["i"], "ts": m["ts"], "stop_reason": m["stop_reason"],
                "tool": tool, "target": target, "tools": tools_list, "toolUseIds": tu_ids,
                "input": inp, "cacheCreation": cc, "cacheRead": cr, "output": out,
                "usageIsReal": real, "toolErrors": tool_errors,
            })
        for t in turns:
            rc = sum(result_chars.get(tid, 0) for tid in t["toolUseIds"])
            t["resultChars"] = rc or None
            t["burden"] = (t["input"] + t["cacheCreation"]) if t["usageIsReal"] else rc   # 剔 cacheRead (重读已缓存=会话体量非本 turn 增量); 显示的 ctx 仍 = input+cacheRead 保真 (app.js renderTurnRow)
            del t["toolUseIds"]
        burdens = [t["burden"] for t in turns]
        mean_b = sum(burdens) / len(burdens) if burdens else 0
        for t in turns:
            t["outlier"] = bool(mean_b > 0 and (t["burden"] or 0) > 1.5 * mean_b)
        return {"turns": turns, "n": len(turns), "limit": limit}
    except Exception:
        return empty


def agent_spawn_head(root_path, agent_id, agent_path=None, limit=200000):
    """重扫 root <sid>.jsonl 找 toolUseResult.agentId == agent_id → spawn 头聚合 (§8.6 Level 3 头, 全真).

    字段来自 root 打包 toolUseResult (§8.6 边界1: spawn 头全真, root 打包):
      agentType / dur / tokens{total,hit,cacheRead,input,output} / toolStats / prompt 摘要 /
      totalToolUseCount / resolvedModel.
    hit = cacheRead/(input+cc+cr) ×100 (input-side, §8.3 口径).
    prompt 摘要 = 首 100 字符 (spawn 详情 on-demand per-spawn summary; 全文 task 是 turn 原文).
    agent_path 用于 resolvedModel 回退 (_resolve_model_fallback).

    bulletproof: 未命中/异常 → None (不抛)."""
    if not root_path or not agent_id or not os.path.isfile(root_path):
        return None
    afm = {agent_id: agent_path} if agent_path else {}
    try:
        with open(root_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
                tur = obj.get("toolUseResult")
                if not isinstance(tur, dict) or tur.get("agentId") != agent_id:
                    continue
                # token 权威源同 _build_record: agent 文件终态累计 > root 末轮 (一致性 —— 否则 spawn
                # 头与 fleet 表显不同数, 用户已多次抓此类不一致).
                meta = _agent_meta(agent_id, afm)
                af_model, af_sum = _agent_file_stats(agent_id, afm)
                if af_sum and (af_sum["cacheRead"] + af_sum["cacheCreation"] + af_sum["input"]) > 0:
                    inp = af_sum["input"]; cc = af_sum["cacheCreation"]
                    cr = af_sum["cacheRead"]; out = af_sum["output"]
                    total = (inp + out + cc + cr) or None
                else:
                    usage = tur.get("usage") or {}
                    inp = _token_or_none(usage, "input_tokens") or 0
                    cc = _token_or_none(usage, "cache_creation_input_tokens") or 0
                    cr = _token_or_none(usage, "cache_read_input_tokens") or 0
                    out = _token_or_none(usage, "output_tokens") or 0
                    total = tur.get("totalTokens")
                den = inp + cc + cr
                prompt = tur.get("prompt")
                prompt_summary = None
                prompt_chars = None
                if isinstance(prompt, str):
                    prompt_chars = len(prompt)
                    prompt_summary = (prompt[:100] + "…") if len(prompt) > 100 else prompt
                return {
                    "agentId": agent_id,
                    "agentType": tur.get("agentType") or (meta or {}).get("agentType") or "unknown",
                    "status": tur.get("status"),
                    "totalDurationMs": tur.get("totalDurationMs"),
                    "resolvedModel": tur.get("resolvedModel") or af_model,
                    "tokens": {"total": total, "input": inp,
                               "cacheCreation": cc, "cacheRead": cr, "output": out},
                    "hit": round(cr / den * 100, 1) if den > 0 else None,
                    "toolStats": tur.get("toolStats") or {},
                    "totalToolUseCount": tur.get("totalToolUseCount"),
                    "toolErrorCount": (count_tool_errors(agent_path)["count"] if agent_path else 0),   # §8.6 ✗ tool 失败 (该 spawn 自己的 tool_result is_error 计数; 与 status 分轨, 不并 successRate)
                    "promptChars": prompt_chars,
                    "promptSummary": prompt_summary,
                }
    except Exception:
        return None
    return None


def agent_turn_raw(path, turn_index, limit=200000):
    """读 agent-<id>.jsonl 第 turn_index 个 assistant turn 的原文 (§8.6 logs, F9 on-demand).

    与 agent_turn_traces 同索引 (assistant turn 计数, 0-based). 返回该 turn 的:
      blocks[]  = assistant message.content (text 块 + tool_use 块含 input 全文)
      results[] = 配对 (tool_use_id 匹配) 的 user tool_result 块 raw content (str/list 原样)
      usage / stop_reason / ts.
    **跨 F9 deliberately**: 全文 input/result/text 是 turn 原文 设计 (§8.6 工程形态: raw 通道与聚合
    管线物理分开). raw=True 标记供前端显 '本地原始内容'.

    bulletproof: 越界/异常 → None."""
    if (not path or turn_index is None or turn_index < 0
            or not os.path.isfile(path)):
        return None
    try:
        # A2: turn_index = message 序号 (与 agent_turn_traces 同空间). 返回该 message 全部 content blocks
        # (同 message.id 跨多行 jsonl flatten, 按行序/行内序) + 全部 tool_use_id 的配对 tool_result.
        messages = _collect_turns_by_message(path)
        if turn_index >= len(messages):
            return None
        m = messages[turn_index]
        blocks, tu_ids = [], []
        for blk in m["blocks"]:
            bt = blk.get("type")
            if bt == "text":
                blocks.append({"type": "text", "text": blk.get("text")})
            elif bt == "tool_use":
                tu_ids.append(blk.get("id"))
                blocks.append({"type": "tool_use", "id": blk.get("id"),
                               "name": blk.get("name"), "input": blk.get("input")})
        # 全文索引 tool_result (真 transcript tool_use 与 tool_result 常空间分离, 局部 capture 会系统性漏)
        result_map = {}
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if not isinstance(obj, dict) or obj.get("type") != "user":
                    continue
                msg = obj.get("message")
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                for blk in content:
                    if isinstance(blk, dict) and blk.get("type") == "tool_result":
                        tid = blk.get("tool_use_id")
                        if tid and tid not in result_map:
                            result_map[tid] = {"type": "tool_result", "toolUseId": tid,
                                               "isError": bool(blk.get("is_error")),
                                               "content": blk.get("content")}
        want_ids = set(tu_ids)
        return {
            "turnIndex": turn_index, "ts": m["ts"], "stop_reason": m["stop_reason"],
            "usage": m["usage"], "blocks": blocks,
            "results": [result_map[t] for t in tu_ids if t in result_map and t in want_ids],
            "raw": True,
        }
    except Exception:
        return None


def load_transcript(args):
    """Mode B ingest 入口 (契约同 analyze.load_records): 返回 (records, nfiles, skipped).

    args.transcript 可为:
      - root '<sid>.jsonl' 文件 —— 自动探测同级 '<sid>/subagents/agent-*.jsonl' (嵌套深度);
      - '<sid>/' 目录 —— root 文件取同级 '<sid>.jsonl', subagents 取 '<dir>/subagents/'.
    nfiles 计产出了 record 的文件数 (root + 有 Agent spawn 的 agent 文件).
    """
    target = getattr(args, "transcript", None)
    if not target or not os.path.exists(target):
        return [], 0, 0

    if os.path.isdir(target):
        sid = os.path.basename(os.path.normpath(target))
        parent = os.path.dirname(os.path.normpath(target))
        root_path = os.path.join(parent, sid + ".jsonl")
        subagents_dir = os.path.join(target, "subagents")
        if not os.path.isfile(root_path):
            _warn(f"目录 {target} 下未找到同级 {sid}.jsonl (root transcript).")
            return [], 0, 0
    else:
        root_path = os.path.abspath(target)
        sid = _derive_session_id(root_path)
        session_dir = os.path.join(os.path.dirname(root_path), sid) if sid else None
        subagents_dir = os.path.join(session_dir, "subagents") if session_dir else None
        if not subagents_dir or not os.path.isdir(subagents_dir):
            subagents_dir = None
            _warn(f"未找到同级 {sid}/subagents/ —— 只解析 root 文件 (§9.3#1). "
                  f"注: CC transcript 本就只持久化 root 直发结构化 spawn, depth-2 是 Mode B 上限.")

    session_id = _derive_session_id(root_path) or _read_first_session_id(root_path) or "unknown-session"
    project_name = (getattr(args, "project", None)
                    or os.path.basename(os.path.dirname(os.path.dirname(root_path)))
                    or "transcript")

    # agent_file_map: spawnedId → path (resolvedModel 回退用); files_to_scan: (path, caller_agent_id, is_root)
    agent_file_map = {}
    files_to_scan = [(root_path, None, True)]
    if subagents_dir and os.path.isdir(subagents_dir):
        for fname in sorted(os.listdir(subagents_dir)):
            if fname.startswith("agent-") and fname.endswith(".jsonl") and ".meta." not in fname:
                aid = fname[len("agent-"):-len(".jsonl")]
                fpath = os.path.join(subagents_dir, fname)
                agent_file_map[aid] = fpath
                files_to_scan.append((fpath, aid, False))   # caller = 文件归属 (§9.2)

    all_records, nfiles, skipped = [], 0, 0
    for fpath, caller_aid, is_root in files_to_scan:
        recs, sk = parse_transcript_file(fpath, caller_aid, is_root,
                                         session_id, project_name, agent_file_map)
        if recs:
            nfiles += 1
        all_records += recs
        skipped += sk
    return all_records, nfiles, skipped
