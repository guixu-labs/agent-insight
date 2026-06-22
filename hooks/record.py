"""PostToolUse recorder: 被动记录 Agent / Skill / Bash 事件 → 滚动 JSONL.

Phase 1 (design §13):
  - Agent 轨道 (always-on): SubagentCallRecord — per-subagent token / 时延 / 成败 (§6, §7 路径A 无状态).
  - Skill 轨道 (always-on): SkillCallRecord — 哪个 subagent 加载了哪些 capability skill (零 token, F3).
  - Bash 轨道 (opt-in, 默认关): CommandRecord — verify/校验命令 interrupted + stderr
    (F5: payload 无 exit code, 结果邻近信号降级为 interrupted + stderr 文本).
  - 横切: 滚动 JSONL (按天 / project) + flock 防并发 append (§12#4).

跨 session 续接 (Phase 3 才建 SessionStart hook + lineage log):
  本 recorder 已盖 generationId = effective_id (carrier ? carrier : sessionId),
  复用 /tmp/cont-probe 原型验证过的 carrier 读取逻辑 (36/36 断言过, §13).
  无 SessionStart hook 时 per-event 读 carrier (carrier 整 session 不变, 功能等价);
  Phase 3 加 SessionStart 建映射 + lineage log, 本段 carrier 读取逻辑不变.

拓扑归属 (§7 路径A, 无状态独立落盘):
  caller = 顶层 agent_id (有 = 子 agent 发起; 缺失 = root 直发);
  spawned = tool_response.agentId / agentType;
  parent / callChain 是离线 reader 从 caller ↔ spawned 匹配派生的视图, 不在线算.

红线: 观测只量不动 —— 任何异常 swallow + exit 0, 绝不阻断编排.
"""
import json
import os
import sys
import fcntl
from datetime import datetime, timezone, timedelta

# 本地时区 (开发机); timestamp 落本地时区, 与 §6 / Phase 0 样本一致.
_TZ = timezone(timedelta(hours=8))


# ---------- carrier / effective_id (搬 /tmp/cont-probe/sessionstart_hook.py, 36/36 验过) ----------
def read_carrier():
    """返回 (generationId, carrierSource); 无 carrier 则 (None, None).

    载体二选一, env 优先: ① env AGENTINSIGHT_CARRIER_ID; ② handoff 文件 AGENTINSIGHT_CARRIER_FILE.
    """
    g = os.environ.get("AGENTINSIGHT_CARRIER_ID", "").strip()
    if g:
        return g, "env"
    hf = os.environ.get("AGENTINSIGHT_CARRIER_FILE", "")
    if hf and os.path.exists(hf):
        try:
            with open(hf) as f:
                d = json.load(f)
            g = (d.get("generationId") or "").strip()
            if g:
                return g, "handoff-file"
        except Exception:
            pass
    return None, None


# ---------- 滚动 JSONL (按天 / project) + flock ----------
def _project_name(cwd):
    p = os.environ.get("AGENTINSIGHT_PROJECT", "").strip()
    if p:
        return p
    if cwd:
        base = os.path.basename(os.path.abspath(cwd))
        if base:
            return base
    return "default"


def _log_dir(cwd):
    """logDir 优先级: env AGENTINSIGHT_LOG_DIR > CLAUDE_PLUGIN_DATA > ~/.claude/agent-insight."""
    base = (os.environ.get("AGENTINSIGHT_LOG_DIR", "").strip()
            or os.environ.get("CLAUDE_PLUGIN_DATA", "").strip()
            or os.path.expanduser("~/.claude/agent-insight"))
    return os.path.join(base, _project_name(cwd))


def write_record(record, cwd):
    """flock 保护下 append 一行到 <logDir>/<project>/YYYY-MM-DD.jsonl (§12#4 并发竞态)."""
    d = _log_dir(cwd)
    os.makedirs(d, exist_ok=True)
    fname = datetime.now(_TZ).strftime("%Y-%m-%d") + ".jsonl"
    path = os.path.join(d, fname)
    lock_path = path + ".lock"
    line = json.dumps(record, ensure_ascii=False)
    with open(lock_path, "a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            with open(path, "a") as f:
                f.write(line + "\n")
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


# ---------- 三轨 record 构造 ----------
def _common(data):
    """三轨共享字段 (§6). 在线只落原始事实; parent/callChain 留给离线 reader 派生."""
    session_id = data.get("session_id", "") or "unknown-session"
    cwd = data.get("cwd", "") or ""
    carrier, carrier_src = read_carrier()
    effective_id = carrier if carrier else session_id
    caller_id = data.get("agent_id") or None       # §7/F8: 顶层 agent_id = caller; 缺失 = root
    caller_type = data.get("agent_type") or None
    return {
        "schemaVersion": 1,
        "timestamp": datetime.now(_TZ).isoformat(timespec="seconds"),
        "runId": session_id,                        # Phase 1 默认 = sessionId (item8 user-turn 待 live 验收定终)
        "generationId": effective_id,               # effective_id = carrier ? carrier : sessionId (续接就绪)
        "carrierSource": carrier_src,               # 诊断: env / handoff-file / null
        "projectName": _project_name(cwd),
        "sessionId": session_id,
        "toolUseId": data.get("tool_use_id") or None,
        "caller": {
            "agentId": caller_id,
            "agentType": caller_type,
            "isRoot": caller_id is None,            # §7: caller 缺失 = 根调用 (orchestrator 是角色标签, 非写死 agent 名)
        },
        "budgetState": None,                        # Phase 3 才算 (§6 seam); Phase 1 恒 null
    }


# ---------- agent transcript 终态累计 (live token 权威源, 与 transcript_adapter 同核) ----------
_terminal_stats_fn = None   # lazy cache (per-process); Skill/Bash fire 不付 import 开销


def _agent_terminal(transcript_path, session_id, agent_id):
    """completed (同步) agent → 其 transcript 终态累计 (token 权威源). 单一计费口径 (与离线同核).

    PostToolUse(Agent) 在 status=completed 时触发 = agent 已 end_turn, 其 subagents/agent-<id>.jsonl
    已写完 → 读得到真实累计 (末轮 usage 1.7x-17x 低估; async 更恒 None). async_launched 不走此路
    (文件未写完 → 终态块缺), 由读端 (analyze.py _reconcile_live_records) 扫 subagents/ 补全 (刷新即最新).

    路径: transcript_path (hook common 字段 = 主线 session transcript) 派生
      dirname(transcript_path)/<sid>/subagents/agent-<agentId>.jsonl (镜像 server._agent_path);
      transcript_path 缺 → glob ~/.claude/projects/*/<sid>/subagents/agent-<agentId>.jsonl 兜底.
    返回 (model | None, usage_dict | None); 无文件/异常 → (None, None). 永不抛 (hook 红线)."""
    global _terminal_stats_fn
    if _terminal_stats_fn is None:
        try:
            _tools_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
            if _tools_dir not in sys.path:
                sys.path.insert(0, _tools_dir)
            from terminal_stats import terminal_stats as _fn
            _terminal_stats_fn = _fn
        except Exception:
            _terminal_stats_fn = False   # 标记不可用 (import 失败), 不再重试
    fn = _terminal_stats_fn
    if not fn:
        return None, None
    path = None
    if transcript_path and session_id and agent_id:
        cand = os.path.join(os.path.dirname(transcript_path), session_id, "subagents", f"agent-{agent_id}.jsonl")
        if os.path.isfile(cand):
            path = cand
    if path is None and session_id and agent_id:
        try:
            import glob as _glob
            hits = _glob.glob(os.path.join(os.path.expanduser("~/.claude/projects"),
                                           "*", session_id, "subagents", f"agent-{agent_id}.jsonl"))
            if hits:
                path = hits[0]
        except Exception:
            pass
    if not path:
        return None, None
    try:
        return fn(path)
    except Exception:
        return None, None


def _caller_turn(transcript_path, tool_use_id):
    """D10: 反查含 tool_use_id 的 assistant 行序号 (Skill callerTurn 锚点).

    口径与 D6 (transcript_adapter parse) / agent_turn_traces 同谓词 (D14 不变量):
      每 type=="assistant" 且 message 是 dict 的行 +1, 无 usage 过滤 / 无去重.
    命中该行内 message.content (结构化 dict 列表, 非伪 XML — F9 不违反) 含 block.type=="tool_use"
    且 block.id==tool_use_id 的, 返回当时计数 (绑定到含 tool_use 的行, 非末行 — 多行消息下与 D6 一致).

    Skill 的 tool_result 回来才 fire PostToolUse(Skill) hook → 触发它的 assistant turn 必已 flush
    到 transcript → 反查可靠 (race 风险低). transcript_path = caller 自身 transcript (root 直调 = 主线
    session jsonl; 子 agent 内调 = 该 agent 的 subagents/agent-<id>.jsonl), 故直读该路径, 无需派生.

    全程 try/except → None (hook 红线: 永不抛 / 永不阻塞 always-on Skill 轨道); 缺参 / 无文件 /
    未命中 / 坏行 → None (诚实缺省, 前端按 None 不渲染 turn chip)."""
    if not transcript_path or not tool_use_id:
        return None
    try:
        if not os.path.isfile(transcript_path):
            return None
        idx = -1
        with open(transcript_path) as f:
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
                idx += 1
                content = msg.get("content")
                if isinstance(content, list):
                    for _b in content:
                        if isinstance(_b, dict) and _b.get("type") == "tool_use" and _b.get("id") == tool_use_id:
                            return idx
        return None
    except Exception:
        return None


def record_agent(data):
    """Agent 轨道 → SubagentCallRecord (核心 / MVP, per-subagent token).

    token 权威源 (2026-06-19 定调, 与 transcript_adapter 同核): status=completed (同步) → 读 agent
    transcript 终态累计 (tokenSource=agentFile); 读不到则回退 tool_response.usage 末轮 (tokenSource=lastTurn,
    低估). status=async_launched → 末轮/None 占位 (capturePhase=launch). 异步/历史记录的终态累计由读端
    (analyze.py _reconcile_live_records) 扫 subagents/ 就地补全 (tokenSource→agentFile, capturePhase→complete);
    每 agentId 仅一条 PostToolUse 记录 → 补全到位即可, 不需去重. dashboard 是 passive reader, 刷新即最新,
    故读端补全 = 与 sweep hook 同结果但零编排开销 / 零状态文件 / 零重复写."""
    ti = data.get("tool_input", {}) or {}
    tr = data.get("tool_response", {}) or {}
    usage = tr.get("usage", {}) or {}
    rec = _common(data)
    rec["recordType"] = "SubagentCall"
    rec["subagentType"] = ti.get("subagent_type") or tr.get("agentType") or "unknown"
    rec["spawned"] = {
        "agentId": tr.get("agentId"),
        "agentType": tr.get("agentType"),
    }
    status = tr.get("status")
    agent_id = tr.get("agentId")

    # 末轮占位 (tool_response.usage; 同步/异步都带, 异步恒 0/None)
    t_in = usage.get("input_tokens")
    t_out = usage.get("output_tokens")
    t_cc = usage.get("cache_creation_input_tokens")
    t_cr = usage.get("cache_read_input_tokens")
    t_total = tr.get("totalTokens")
    token_source = "lastTurn" if (t_cr or t_cc or t_in) else "none"
    file_model = None

    # 同步完成 → 覆盖为 agent 文件终态累计 (真值, 与离线同口径)
    if status == "completed" and agent_id:
        file_model, usum = _agent_terminal(data.get("transcript_path"), data.get("session_id", ""), agent_id)
        if usum and (usum["cacheRead"] + usum["cacheCreation"] + usum["input"]) > 0:
            t_in = usum["input"]
            t_out = usum["output"]
            t_cc = usum["cacheCreation"]
            t_cr = usum["cacheRead"]
            t_total = (usum["input"] + usum["output"] + usum["cacheCreation"] + usum["cacheRead"]) or None
            token_source = "agentFile"

    rec["tokens"] = {
        "input": t_in,
        "output": t_out,
        "cacheCreation": t_cc,
        "cacheRead": t_cr,
        "total": t_total,
    }
    rec["tokenSource"] = token_source                       # agentFile(真值) / lastTurn(末轮低估) / none
    rec["capturePhase"] = "complete" if status == "completed" else "launch"   # 读端去重键: complete > launch
    # §6: Phase 0 样本 顶层 duration_ms(=6418) 与 tool_response.totalDurationMs(=6386) 差 32ms,
    # 未表征是 hook 开销还是别的. Phase 1 先取 totalDurationMs (子 agent 内部耗时, 更贴近 per-subagent 归因),
    # live 验收厘清两路口径后定终.
    rec["durationMs"] = tr.get("totalDurationMs")
    rec["resolvedModel"] = tr.get("resolvedModel") or file_model   # F6: 末轮 model; 回退 agent 文件首条 model
    rec["success"] = (status == "completed")
    rec["error"] = None if status == "completed" else status  # error-case payload 形态待 live 补 (§6)
    return rec


def record_skill(data):
    """Skill 轨道 → SkillCallRecord (零 token, F3)."""
    ti = data.get("tool_input", {}) or {}
    tr = data.get("tool_response", {}) or {}
    rec = _common(data)
    rec["recordType"] = "SkillCall"
    # skill 名: tool_response.commandName (F3 实证带, 形如 superpowers:executing-plans) 优先; fallback tool_input.
    rec["skillName"] = tr.get("commandName") or ti.get("skill") or ti.get("command") or "unknown"
    rec["success"] = bool(tr.get("success"))
    rec["tokens"] = None     # 零 token (F3): Skill 只能追踪"加载了哪些", 不能归因 token 成本
    rec["callerTurn"] = _caller_turn(data.get("transcript_path"), data.get("tool_use_id"))   # D10: drillTurn 锚点 (None=未绑定, 不阻塞)
    return rec


def record_bash(data):
    """Bash 轨道 → CommandRecord (opt-in, F5: 无 exit code → interrupted + stderr)."""
    ti = data.get("tool_input", {}) or {}
    tr = data.get("tool_response", {}) or {}
    rec = _common(data)
    rec["recordType"] = "Command"
    rec["command"] = ti.get("command") or ""
    rec["interrupted"] = bool(tr.get("interrupted"))
    rec["stderr"] = tr.get("stderr") or ""             # 结果邻近信号 (F5: 无 exit code, 降级 interrupted + stderr)
    rec["exitCode"] = None                              # F5: payload 无 exit code 字段, 恒 null
    return rec


# ---------- 主入口 ----------
def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)   # 烂 stdin: 静默 no-op, 不阻断

    tool_name = data.get("tool_name", "") or ""

    try:
        if tool_name == "Agent":
            rec = record_agent(data)
        elif tool_name == "Skill":
            rec = record_skill(data)
        elif tool_name == "Bash":
            # opt-in gate (§13: Bash 高频 × fork-exec 会拖慢编排, 默认关)
            if os.environ.get("AGENTINSIGHT_BASH", "").strip() not in ("1", "true", "yes"):
                sys.exit(0)
            rec = record_bash(data)
        else:
            sys.exit(0)   # 非三轨: no-op
        write_record(rec, data.get("cwd", ""))
    except Exception:
        # 红线: 观测绝不阻断编排 —— 任何异常 swallow (磁盘满 / 字段缺 / 并发冲突)
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
