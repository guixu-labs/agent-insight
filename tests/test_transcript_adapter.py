#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent-insight Mode B transcript adapter (tools/transcript_adapter.py + analyze.py --transcript) 单测.

【隔离保证】(同 test_analyze.py / test_record.py 打法)
  - 合成 transcript-shaped JSONL 行写进 tempfile, 子进程 `analyze.py --transcript <root> --json` 跑;
  - 不碰真 ~/.claude / 真 transcript / 真 session / settings.json / marketplace.json.
  => 对当前 session 零影响.

测的是 Mode B ingest: transcript toolUseResult → §6 record → 复用 to_event/build_topology/...
  root 直发 / token snake→camel / resolvedModel 三态 (present/fallback/null) / success 派生 /
  isRoot 不变量 / depth-2-only warning / 合成 depth-3 (锁 flat-scan depth-general) /
  非 Agent skip / totalTokens 缺求和回退 / 空文件 / CLI 端到端.

平台边界 (§9.3, 真 CC transcript 零嵌套可推广): Mode B 只重建 depth-2 (root→agent).
本组 test 9 用合成 agent-<X>.jsonl 含结构化 spawn 锁 flat-scan 的 depth-general 正确性 ——
真 CC transcript 不产此形态 (agent 文件该行 toolUseResult=null), 故 depth-3 仅理论上可达.
"""
import json
import os
import sys
import shutil
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ANALYZE = os.path.join(HERE, "..", "tools", "analyze.py")

SID_A = "s-test"   # 组15 root transcript sid (default run_transcript sid)

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}   {detail}")


# ---------- 合成 transcript 行构造器 ----------
def agent_result_line(agent_id, agent_type="Explore", status="completed",
                      total_tokens=10000, inp=1000, out=500, ccr=0, cr=8500,
                      dur_ms=5000, resolved_model=None, message_model=None,
                      ts="2026-06-16T13:00:00+08:00", is_sidechain=False,
                      session_id="s-test"):
    """一条带 Agent toolUseResult (dict + agentId) 的 transcript 行. total_tokens=None → 省略 totalTokens."""
    tur = {
        "status": status, "agentId": agent_id, "agentType": agent_type,
        "totalDurationMs": dur_ms, "usage": {
            "input_tokens": inp, "output_tokens": out,
            "cache_creation_input_tokens": ccr, "cache_read_input_tokens": cr,
        },
    }
    if total_tokens is not None:
        tur["totalTokens"] = total_tokens
    if resolved_model is not None:
        tur["resolvedModel"] = resolved_model
    msg = {"role": "assistant"}
    if message_model is not None:
        msg["model"] = message_model
    return json.dumps({
        "timestamp": ts, "sessionId": session_id, "isSidechain": is_sidechain,
        "type": "assistant", "uuid": "u-" + agent_id, "message": msg,
        "toolUseResult": tur,
    }, ensure_ascii=False)


def assistant_model_line(model, is_sidechain=True, session_id="s-test",
                         ts="2026-06-16T13:00:01+08:00"):
    """一条带 assistant message.model 的行 (resolvedModel 回退源)."""
    return json.dumps({
        "timestamp": ts, "sessionId": session_id, "isSidechain": is_sidechain,
        "type": "assistant", "message": {"role": "assistant", "model": model, "content": []},
    }, ensure_ascii=False)


_SKILL_SEQ = [0]   # 全局递增 → tool_use_id / message.id 跨调用唯一 (多次同名 skill 不覆写 pending)


def skill_result_line(command_name, success=True, allowed_tools=None,
                      ts="2026-06-16T13:05:00+08:00"):
    """一条 ROOT Skill 调用 = assistant tool_use(Skill) + user tool_result(带顶层 tur) 两行
    (镜像 real CC; D4 单源 tool_use 采集 + success 反查). 返回 newline-joined 2 行 jsonl.

    skillName 取自 tool_use.input.skill; success 从配对顶层 tur.success 反查 (root 真 success).
    无 agentId (Skill 非 Agent) → 不进 SubagentCall 分支."""
    _SKILL_SEQ[0] += 1
    n = _SKILL_SEQ[0]
    tuid = "tu-skill-%d" % n
    tur = {"success": success, "commandName": command_name,
           "allowedTools": allowed_tools if allowed_tools is not None else []}
    assistant_line = json.dumps({
        "timestamp": ts, "sessionId": "s-x", "isSidechain": False,
        "type": "assistant", "uuid": "u-skill-%d" % n,
        "message": {"role": "assistant", "id": "msg-skill-%d" % n, "content": [
            {"type": "tool_use", "id": tuid, "name": "Skill", "input": {"skill": command_name}},
        ]},
    }, ensure_ascii=False)
    user_line = json.dumps({
        "timestamp": ts, "sessionId": "s-x", "isSidechain": False,
        "type": "user", "uuid": "u-skillres-%d" % n,
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tuid},
        ]},
        "toolUseResult": tur,
    }, ensure_ascii=False)
    return assistant_line + "\n" + user_line


def spawn_skill_line(command_name, ts="2026-06-16T13:05:00+08:00"):
    """一条 SPAWN Skill 调用 = 仅 assistant tool_use(Skill) (无 user tool_result / 无顶层 tur),
    镜像 real spawn transcript (不写顶层 toolUseResult). parse 经 EOF flush 产 SkillCall,
    success=None 诚实缺省 (spawn 无 tur 可反查). caller.isRoot=False (文件归属)."""
    _SKILL_SEQ[0] += 1
    n = _SKILL_SEQ[0]
    tuid = "tu-spawn-skill-%d" % n
    return json.dumps({
        "timestamp": ts, "sessionId": "s-x", "isSidechain": True,
        "type": "assistant", "uuid": "u-spawn-skill-%d" % n,
        "message": {"role": "assistant", "id": "msg-spawn-skill-%d" % n, "content": [
            {"type": "tool_use", "id": tuid, "name": "Skill", "input": {"skill": command_name}},
        ]},
    }, ensure_ascii=False)


_AGENT_SEQ = [0]   # 全局递增 → tool_use_id/message.id 跨调用唯一


def agent_call_lines(agent_id, agent_type="Explore", in_msg_id=None,
                     ts_use="2026-06-16T13:00:00+08:00", ts_res="2026-06-16T13:00:05+08:00"):
    """Agent spawn 真实两行 (镜像 real CC): assistant(tool_use Task, id=tu-ag-N) + 后续
    user(tool_result 配对 tool_use_id + 顶层 toolUseResult 带 agentId → SubagentCall).

    tool_use 落在 message.id=in_msg_id 的 assistant 行 (调用方传, 可置入多 message 场景验证 callerTurn
    绑该 message 序号). parse 经 tu_turn 反查得 callerTurn = 该 message 序号 (拓扑锚点; 与 Skill 同口径)."""
    _AGENT_SEQ[0] += 1
    n = _AGENT_SEQ[0]
    tuid = "tu-agent-%d" % n
    mid = in_msg_id or ("msg-agent-%d" % n)
    assistant_line = json.dumps({
        "timestamp": ts_use, "sessionId": "s-x", "isSidechain": False,
        "type": "assistant", "uuid": "u-ag-%d" % n,
        "message": {"role": "assistant", "id": mid, "content": [
            {"type": "tool_use", "id": tuid, "name": "Task", "input": {"description": "x"}},
        ]},
    }, ensure_ascii=False)
    tur = {"status": "completed", "agentId": agent_id, "agentType": agent_type,
           "totalDurationMs": 5000, "usage": {"input_tokens": 1000, "output_tokens": 500,
           "cache_creation_input_tokens": 0, "cache_read_input_tokens": 8500}, "totalTokens": 10000}
    user_line = json.dumps({
        "timestamp": ts_res, "sessionId": "s-x", "isSidechain": False,
        "type": "user", "uuid": "u-agres-%d" % n,
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tuid},
        ]},
        "toolUseResult": tur,
    }, ensure_ascii=False)
    return assistant_line + "\n" + user_line


def nonagent_string_line(session_id="s-test"):
    """非 Agent: toolUseResult 为字符串 → 须 skip."""
    return json.dumps({
        "timestamp": "2026-06-16T13:00:02+08:00", "sessionId": session_id,
        "isSidechain": False, "type": "user", "toolUseResult": "some file content",
    }, ensure_ascii=False)


def nonagent_null_line(session_id="s-test"):
    """非 Agent: toolUseResult 为 null → 须 skip."""
    return json.dumps({
        "timestamp": "2026-06-16T13:00:03+08:00", "sessionId": session_id,
        "isSidechain": False, "type": "assistant", "toolUseResult": None,
    }, ensure_ascii=False)


def run_transcript(root_lines, subagents=None, sid="s-test", json_mode=True, extra=None):
    """合成 transcript 布局 → analyze.py --transcript <root> [--json].
    root_lines: 写进 <tmp>/<sid>.jsonl 的行 (list[str]).
    subagents: {agent_id: [lines]} → 写进 <tmp>/<sid>/subagents/agent-<agent_id>.jsonl.
    返回 {_rc, _stderr, _stdout} + (json 模式下) 解析字段."""
    d = tempfile.mkdtemp(prefix="obs-tr-")
    root_fp = os.path.join(d, sid + ".jsonl")
    with open(root_fp, "w") as f:
        for ln in root_lines:
            f.write(ln + "\n")
    if subagents:
        sub_dir = os.path.join(d, sid, "subagents")
        os.makedirs(sub_dir, exist_ok=True)
        for aid, lines in subagents.items():
            with open(os.path.join(sub_dir, "agent-%s.jsonl" % aid), "w") as f:
                for ln in lines:
                    f.write(ln + "\n")
    cmd = [sys.executable, ANALYZE, "--transcript", root_fp]
    if json_mode:
        cmd += ["--json"]
    if extra:
        cmd += extra
    p = subprocess.run(cmd, capture_output=True, text=True)
    res = {"_rc": p.returncode, "_stderr": p.stderr, "_stdout": p.stdout}
    if json_mode and p.returncode == 0:
        try:
            res.update(json.loads(p.stdout))
        except Exception:
            pass
    shutil.rmtree(d, ignore_errors=True)
    return res


def find_chain(res, subagent_type, session_id=None):
    for c in res.get("callChains", []):
        if c.get("subagentType") == subagent_type:
            if session_id is None or c.get("sessionId") == session_id:
                return c
    return None


print("=" * 70)
print("agent-insight Mode B transcript adapter 单测 (隔离)")
print("=" * 70)

# ===== 组1 root 直发单条 (Mode B 基本形态) =====
print("\n[组1] root 直发 — transcript toolUseResult → §6 record → chain")
res = run_transcript([agent_result_line("a1", "general-purpose", total_tokens=12805,
                                        inp=11843, out=2, cr=960, dur_ms=6610)])
check("落盘 1 条解析", res.get("recordsTotal") == 1, res)
check("byTrack SubagentCall=1", res.get("byTrack", {}).get("SubagentCall") == 1, res.get("byTrack"))
check("modeLabel=B · transcript", res.get("modeLabel") == "B · transcript", res.get("modeLabel"))
check("sessions=1", len(res.get("sessions", [])) == 1, res.get("sessions"))
check("grandTotal.total=12805", res.get("grandTotal", {}).get("total") == 12805, res.get("grandTotal"))
c = find_chain(res, "general-purpose")
check("chain=[orchestrator, general-purpose]", c and c.get("callChain") == ["orchestrator", "general-purpose"], c)
check("parentType=orchestrator", c and c.get("parentType") == "orchestrator", c)
check("depth=2", c and c.get("depth") == 2, c)
check("orphan=False", c and c.get("orphan") is False, c)
check("consistent=True (isRoot 不变量 hold)", res.get("consistency", {}).get("consistent") is True, res.get("consistency"))

# ===== 组2 token snake→camel + total (镜像 record.py:140-146) =====
print("\n[组2] token snake→camel 映射 — usage 四项 + totalTokens")
res = run_transcript([agent_result_line("a1", "Explore", total_tokens=12805,
                                        inp=11843, out=2, ccr=0, cr=960)])
gt = res.get("grandTotal", {})
check("input=11843 (usage.input_tokens)", gt.get("input") == 11843, gt)
check("output=2 (usage.output_tokens)", gt.get("output") == 2, gt)
check("cacheCreation=0", gt.get("cacheCreation") == 0, gt)
check("cacheRead=960", gt.get("cacheRead") == 960, gt)
check("total=12805 (toolUseResult.totalTokens, 非求和)", gt.get("total") == 12805, gt)
check("durationMs=5000 透传", find_chain(res, "Explore") and find_chain(res, "Explore").get("durationMs") == 5000, "dur")

# ===== 组3 resolvedModel 在 toolUseResult (直接命中) =====
print("\n[组3] resolvedModel present — toolUseResult.resolvedModel 直接用")
res = run_transcript([agent_result_line("a1", "Explore", resolved_model="glm-5.1")])
c = find_chain(res, "Explore")
check("resolvedModel=glm-5.1 (present)", c and c.get("resolvedModel") == "glm-5.1", c)

# ===== 组4 resolvedModel 回退 (toolUseResult 缺, agent 文件 message.model 命中) =====
print("\n[组4] resolvedModel fallback — agent-<spawned>.jsonl 首条 assistant message.model")
res = run_transcript(
    [agent_result_line("a1", "Explore", resolved_model=None)],
    subagents={"a1": [assistant_model_line("glm-5.2")]},
)
c = find_chain(res, "Explore")
check("resolvedModel=glm-5.2 (回退命中)", c and c.get("resolvedModel") == "glm-5.2", c)

# ===== 组5 resolvedModel 全缺 → null =====
print("\n[组5] resolvedModel null — toolUseResult 缺 + 无 agent 文件")
res = run_transcript([agent_result_line("a1", "Explore", resolved_model=None)])
c = find_chain(res, "Explore")
check("resolvedModel=null", c and c.get("resolvedModel") is None, c)

# ===== 组6 success 由 status 派生 =====
print("\n[组6] success 派生 — completed=True, error 态 success=False")
res = run_transcript([
    agent_result_line("a1", "Explore", status="completed", ts="2026-06-16T13:00:00+08:00"),
    agent_result_line("a2", "Explore", status="error", ts="2026-06-16T13:00:01+08:00"),
])
rows = res.get("bySubagentType", [])
r = rows[0] if rows else {}
check("2 条 records", res.get("recordsTotal") == 2, res)
check("successRate=0.5 (1 completed / 2)", r.get("successRate") == 0.5, r)

# ===== 组7 isRoot 不变量 (root 直发 → caller.agentId=null + isRoot=true) → consistent =====
print("\n[组7] isRoot 不变量 — root 文件归属 → caller=null/isRoot=true, consistent=True")
res = run_transcript([
    agent_result_line("a1", "architect", ts="2026-06-16T13:00:00+08:00"),
    agent_result_line("a2", "developer", ts="2026-06-16T13:00:01+08:00"),
])
check("consistent=True (两条 root 直发 isRoot 不变量 hold)", res.get("consistency", {}).get("consistent") is True, res.get("consistency"))
check("orphanChains=0 (都达根)", res.get("consistency", {}).get("orphanChains") == 0, res.get("consistency"))

# ===== 组8 单 root 文件无 subagents/ → depth-2-only warning (§9.3#1) =====
print("\n[组8] 单 root 文件 — 无 subagents/ → depth-2-only warning (§9.3#1)")
res = run_transcript([agent_result_line("a1", "Explore")])
check("recordsTotal=1 (仍解析)", res.get("recordsTotal") == 1, res)
check("stderr 含 §9.3#1 / depth-2 警告", "§9.3#1" in res.get("_stderr", "") or "depth-2" in res.get("_stderr", ""), res.get("_stderr"))

# ===== 组9 合成 depth-3 (root spawn outer, outer 的 agent 文件含结构化 spawn inner) =====
print("\n[组9] 合成 depth-3 — flat-scan depth-general (真 CC transcript 不产此形态, 锁逻辑)")
res = run_transcript(
    [agent_result_line("O1", "architect", total_tokens=500, ts="2026-06-16T13:00:00+08:00")],
    subagents={"O1": [agent_result_line("I1", "Explore", total_tokens=300,
                                        is_sidechain=True, ts="2026-06-16T13:00:01+08:00")]},
)
c_arch = find_chain(res, "architect")
c_exp = find_chain(res, "Explore")
check("2 条 records (root 1 + agent 文件 1)", res.get("recordsTotal") == 2, res)
check("architect (outer) depth=2, caller=root", c_arch and c_arch.get("depth") == 2 and c_arch.get("trigger") == "root", c_arch)
check("Explore (inner) caller=O1 (文件归属)", c_exp and c_exp.get("parentType") == "architect", c_exp)
check("Explore chain=[orchestrator, architect, Explore]", c_exp and c_exp.get("callChain") == ["orchestrator", "architect", "Explore"], c_exp)
check("Explore depth=3", c_exp and c_exp.get("depth") == 3, c_exp)
edges = {(e["parentType"], e["childType"]): e["count"] for e in res.get("callGraph", [])}
check("call graph: orchestrator→architect x1", edges.get(("orchestrator", "architect")) == 1, edges)
check("call graph: architect→Explore x1", edges.get(("architect", "Explore")) == 1, edges)

# ===== 组10 非 Agent toolUseResult (string/null) 被 skip =====
print("\n[组10] 非 Agent skip — string / null / 无 agentId 的 toolUseResult 不产 record")
res = run_transcript([
    agent_result_line("a1", "Explore", ts="2026-06-16T13:00:00+08:00"),
    nonagent_string_line(),
    nonagent_null_line(),
])
check("只 1 条 record (2 条非 Agent 被 skip)", res.get("recordsTotal") == 1, res)
check("byTrack SubagentCall=1", res.get("byTrack", {}).get("SubagentCall") == 1, res.get("byTrack"))

# ===== 组11 totalTokens 缺 → 四项求和回退 =====
print("\n[组11] totalTokens 缺 — 回退 input+output+ccr+cr")
res = run_transcript([agent_result_line("a1", "Explore", total_tokens=None,
                                        inp=1000, out=500, ccr=50, cr=8000)])
check("total=9550 (1000+500+50+8000 求和回退)", res.get("grandTotal", {}).get("total") == 9550, res.get("grandTotal"))

# ===== 组12 --transcript CLI 端到端 (人类输出, 无 --json) =====
print("\n[组12] --transcript CLI 人类输出 — exit 0, 含 grand total / self-consistency")
res = run_transcript([agent_result_line("a1", "general-purpose")], json_mode=False)
check("exit 0", res.get("_rc") == 0, res.get("_rc"))
check("stdout 含 'grand total tokens'", "grand total tokens" in res.get("_stdout", ""), res.get("_stdout"))
check("stdout 含 'self-consistency'", "self-consistency" in res.get("_stdout", ""), res.get("_stdout"))
check("header 标 Mode B", "Mode B" in res.get("_stdout", ""), res.get("_stdout"))

# ===== 组13 空文件 (graceful, 不崩) =====
print("\n[组13] 空文件 — recordsTotal=0, consistent=True, 不崩")
res = run_transcript([])
check("recordsTotal=0", res.get("recordsTotal") == 0, res)
check("grandTotal.total=0", res.get("grandTotal", {}).get("total") == 0, res.get("grandTotal"))
check("consistent=True (无数据即无违例)", res.get("consistency", {}).get("consistent") is True, res.get("consistency"))

# ===== 组14 目录输入 (session-dir 形态, auto-discover root + subagents) =====
print("\n[组14] 目录输入 — '<sid>/' 形态, 同级 root + subagents/")
d = tempfile.mkdtemp(prefix="obs-tr-dir-")
sid = "s-dir"
os.makedirs(os.path.join(d, sid, "subagents"))
with open(os.path.join(d, sid + ".jsonl"), "w") as f:
    f.write(agent_result_line("a1", "general-purpose", total_tokens=400) + "\n")
p = subprocess.run([sys.executable, ANALYZE, "--transcript", os.path.join(d, sid), "--json"],
                   capture_output=True, text=True)
shutil.rmtree(d, ignore_errors=True)
resd = json.loads(p.stdout) if p.returncode == 0 else {}
check("目录输入 exit 0", p.returncode == 0, p.stderr)
check("目录输入解析 1 条", resd.get("recordsTotal") == 1, resd)
check("目录输入 grandTotal.total=400", resd.get("grandTotal", {}).get("total") == 400, resd.get("grandTotal"))

# ===== 组15 · offline Skill 重建 (§8.11/§9.3#7 根因修复) =====
print("\n[组15] transcript Skill → SkillCall record (之前 SkillCall=0 根因 = 无 agentId 被 skip)")
res = run_transcript(
    [agent_result_line("a1", "Explore", total_tokens=1000),
     skill_result_line("deep-research")],
    {}, SID_A,
)
check("exit 0", res.get("_rc") == 0, res.get("_stderr"))
bt = res.get("byTrack", {})
check("byTrack SubagentCall=1", bt.get("SubagentCall") == 1, bt)
check("byTrack SkillCall=1 (重建成功)", bt.get("SkillCall") == 1, bt)
records_total = res.get("recordsTotal", 0)
check("recordsTotal=2 (Agent+Skill)", records_total == 2, records_total)
bs = res.get("bySkill", [])
check("bySkill 1 行", len(bs) == 1, bs)
check("skill=deep-research", bs and bs[0]["skillName"] == "deep-research", bs)
check("skill calls=1", bs and bs[0]["calls"] == 1, bs)
check("skill callerTypes orchestrator×1 (root 文件归属)",
      bs and bs[0]["callerTypes"] == {"orchestrator": 1}, bs and bs[0]["callerTypes"])

# ===== 组16 · Skill 零 token (F3) —— 不入 grandTotal =====
print("\n[组16] Skill tokens=None → grandTotal 不含 skill")
res = run_transcript(
    [agent_result_line("a1", "Explore", total_tokens=5000, inp=1000, out=500, cr=3500),
     skill_result_line("deep-research"),
     skill_result_line("superpowers:brainstorming")],
    {}, SID_A,
)
gt = res["grandTotal"]
check("grandTotal total=5000 (两 skill 不入账)", gt["total"] == 5000, gt)
check("grandTotal input=1000", gt["input"] == 1000, gt)
bs = res.get("bySkill", [])
check("bySkill 2 行", len(bs) == 2, bs)
for r in bs:
    check(f"skill {r['skillName']} 行无 token 字段",
          "total" not in r and "input" not in r, list(r.keys()))

# ===== 组17 · depth-2 spawn skill (§8.11/§8.8 边界: spawn 无顶层 tur → success=None 诚实缺省) =====
print("\n[组17] spawn 内 skill (无顶层 tur) → SkillCall success=None, callerType=spawned type")
res = run_transcript(
    [agent_result_line("a1", "Explore", total_tokens=1000)],   # root spawn a1(Explore)
    {"a1": [spawn_skill_line("deep-research")]},                # skill 在 a1 内调 (spawn 无 tur)
    SID_A,
)
bs = res.get("bySkill", [])
check("bySkill 1 行", len(bs) == 1, bs)
check("skill callerTypes Explore×1 (spawned 映射解析, 非 orchestrator)",
      bs and bs[0]["callerTypes"] == {"Explore": 1}, bs and bs[0]["callerTypes"])

# 直接 parse spawn 文件验证 D4 spawn 路径 (success=None 诚实缺省 + isRoot=False; by_skill 不暴露 success)
sys.path.insert(0, ANALYZE.rsplit("/", 1)[0])
from transcript_adapter import parse_transcript_file  # noqa: E402
_d = tempfile.mkdtemp()
_sp = os.path.join(_d, "agent-a1.jsonl")
with open(_sp, "w") as _f:
    _f.write(spawn_skill_line("deep-research") + "\n")
_srecs, _ = parse_transcript_file(_sp, "a1", False, SID_A, "proj", {})
_sk = [r for r in _srecs if r.get("recordType") == "SkillCall"]
check("spawn parse 直采 SkillCall (tool_use 单源)", len(_sk) == 1, len(_sk))
check("spawn skill caller.isRoot=False", _sk and _sk[0]["caller"]["isRoot"] is False, _sk)
check("spawn skill success=None (无 tur 可反查, EOF flush 诚实缺省)",
      _sk and _sk[0]["success"] is None, _sk)
shutil.rmtree(_d, ignore_errors=True)
check("skill calls=1", bs and bs[0]["calls"] == 1, bs)


def test_root_context_samples():
    """R: root_context_samples —— 逐 turn root 主线 context 抽取 (§8.3 数据层, Plan 3a).
    content-safe: 只读 type==assistant 行的 message.usage 四桶, 不读 content."""
    import tempfile
    sys.path.insert(0, os.path.join(HERE, "..", "tools"))
    from transcript_adapter import root_context_samples
    tmp = tempfile.mkdtemp()
    sid = "deadbeef-1111-2222-3333-444455556666"
    p = os.path.join(tmp, sid + ".jsonl")
    lines = [
        # 非 assistant 行 → 跳过 (不计 turn)
        json.dumps({"type": "user", "timestamp": "t0", "message": {"role": "user"}}),
        # assistant turn 0: input 1000 + cacheRead 500 → ctx 1500
        json.dumps({"type": "assistant", "timestamp": "t1", "message": {"role": "assistant",
                   "usage": {"input_tokens": 1000, "cache_creation_input_tokens": 0,
                             "cache_read_input_tokens": 500, "output_tokens": 50}}}),
        # assistant turn 1: input 2000 + cacheRead 4000 → ctx 6000 (peak)
        json.dumps({"type": "assistant", "timestamp": "t2", "message": {"role": "assistant",
                   "usage": {"input_tokens": 2000, "cache_creation_input_tokens": 0,
                             "cache_read_input_tokens": 4000, "output_tokens": 80}}}),
    ]
    with open(p, "w") as f:
        f.write("\n".join(lines) + "\n")
    r = root_context_samples(p)
    check("R1 抽出 2 个 assistant sample (非 assistant 行跳过)", len(r["samples"]) == 2, r.get("samples"))
    check("R1 turn0 ctx = 1500 (input+cc+cr, output 不计)", r["samples"][0]["ctx"] == 1500, r["samples"][0])
    check("R1 turn1 ctx = 6000", r["samples"][1]["ctx"] == 6000, r["samples"][1])
    check("R1 peak = 6000 (max over turns)", r["peak"] == 6000, r.get("peak"))
    check("R1 peakTurn = 1", r["peakTurn"] == 1, r.get("peakTurn"))
    check("R1 ts 透传 (用于 session 视图 曲线 tooltip)", r["samples"][0]["ts"] == "t1", r["samples"][0])
    check("R1 limit 默认 200000 (透明阈值)", r["limit"] == 200000, r.get("limit"))
    # bulletproof: 不存在路径 → 空, 不抛 (per-session 隔离)
    r2 = root_context_samples("/nope/does-not-exist.jsonl")
    check("R1 缺路径 bulletproof (peak 0, 不抛)", r2["peak"] == 0 and r2["samples"] == [], r2)
    # 全非 assistant (如纯 user transcript) → 空
    p2 = os.path.join(tmp, "aaaa1111-2222-3333-4444-555566667777.jsonl")
    with open(p2, "w") as f:
        f.write(json.dumps({"type": "user", "message": {"role": "user"}}) + "\n")
    r3 = root_context_samples(p2)
    check("R1 纯 user transcript → 空 (peak 0)", r3["peak"] == 0 and r3["samples"] == [], r3)

    # bulletproof: 流中混坏 JSON 行 → 跳过该行, 其余 sample 照抽 (per-line 隔离, 不整文件 abort)
    p3 = os.path.join(tmp, "bbbb1111-2222-3333-4444-555566667777.jsonl")
    with open(p3, "w") as f:
        f.write("\n".join([
            json.dumps({"type": "assistant", "timestamp": "t1", "message": {"role": "assistant",
                       "usage": {"input_tokens": 1000, "cache_creation_input_tokens": 0,
                                 "cache_read_input_tokens": 500, "output_tokens": 50}}}),
            "{this is not valid json",   # 坏 JSON 行 (模拟真实 transcript 偶发坏行)
            json.dumps({"type": "assistant", "timestamp": "t2", "message": {"role": "assistant",
                       "usage": {"input_tokens": 2000, "cache_creation_input_tokens": 0,
                                 "cache_read_input_tokens": 4000, "output_tokens": 80}}}),
        ]) + "\n")
    r4 = root_context_samples(p3)
    check("R1 流中坏 JSON 行被跳过 (2 sample 照抽, peak 6000)", len(r4["samples"]) == 2 and r4["peak"] == 6000, r4)

    # bulletproof: message 非 dict (assistant 行但 message 是字符串) → 跳过该行不崩
    p4 = os.path.join(tmp, "cccc1111-2222-3333-4444-555566667777.jsonl")
    with open(p4, "w") as f:
        f.write("\n".join([
            json.dumps({"type": "assistant", "timestamp": "t1", "message": "garbage-string"}),
            json.dumps({"type": "assistant", "timestamp": "t2", "message": {"role": "assistant",
                       "usage": {"input_tokens": 900, "cache_creation_input_tokens": 0,
                                 "cache_read_input_tokens": 100, "output_tokens": 0}}}),
        ]) + "\n")
    r5 = root_context_samples(p4)
    check("R1 message 非 dict 被跳过 (1 sample, peak 1000)", len(r5["samples"]) == 1 and r5["peak"] == 1000, r5)

    # i/turnIndex 锁死语义 (D2.3): 无 usage 的 assistant message 仍占 message 序号 (序号空间与
    # agent_turn_*/parse_transcript_file 同构), 只是不进 ctx sample. 故 sample i = message 序号 0/2 (非 0/1).
    p5 = os.path.join(tmp, "dddd1111-2222-3333-4444-555566667777.jsonl")
    with open(p5, "w") as f:
        f.write("\n".join([
            json.dumps({"type": "assistant", "timestamp": "t1", "message": {"role": "assistant",
                       "usage": {"input_tokens": 1000, "cache_creation_input_tokens": 0,
                                 "cache_read_input_tokens": 500, "output_tokens": 50}}}),
            # assistant message 无 usage → 仍占 message 序号 (i=1), 但不进 ctx sample
            json.dumps({"type": "assistant", "timestamp": "x", "message": {"role": "assistant"}}),
            json.dumps({"type": "assistant", "timestamp": "t2", "message": {"role": "assistant",
                       "usage": {"input_tokens": 2000, "cache_creation_input_tokens": 0,
                                 "cache_read_input_tokens": 4000, "output_tokens": 80}}}),
        ]) + "\n")
    r6 = root_context_samples(p5)
    check("R1 无 usage message 仍占序号 (2 sample, i/turnIndex 0/2 非 0/1)",
          len(r6["samples"]) == 2 and r6["samples"][0]["i"] == 0 and r6["samples"][1]["i"] == 2
          and r6["samples"][0]["turnIndex"] == 0 and r6["samples"][1]["turnIndex"] == 2, r6)


# ===== 组18 · root per-turn context 数据层 (§8.3 / Plan 3a) =====
print("\n[组18] root_context_samples — 逐 turn root 主线 context 抽取 (content-safe)")
test_root_context_samples()

# ===== 组19 · SubagentCall callerTurn (拓扑锚点数据层; depth-2 root / depth-3 父 spawn) =====
print("\n[组19] SubagentCall.callerTurn — 绑 Agent tool_use message 序号 (拓扑锚点; 处理嵌套)")


def test_subagent_call_caller_turn():
    """T-callerTurn: SubagentCall.callerTurn 绑定含该 Agent tool_use 的 assistant **message 序号**
    (A2 空间, 与 Skill callerTurn 同口径), 非时序最近行 / 非 result 行. callerAgentId = 文件归属
    (root 文件 → None=depth-2; 父 spawn 文件 → 父 id=depth-3). 拓扑锚点据此进调用方详情 定位.

    callerTurn 天然 caller 相对: 同一 transcript 作 root 文件解析 (caller=root) 或父 spawn 文件解析
    (caller=父 spawn), callerTurn 同值 (都相对被解析文件) → 处理嵌套 (depth-2 root / depth-3 父 spawn)."""
    import tempfile
    sys.path.insert(0, os.path.join(HERE, "..", "tools"))
    from transcript_adapter import parse_transcript_file
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, "root.jsonl")
    # m0 (纯 text, turn0) + m1 (含 Agent tool_use, turn1); tur 在后续 user 行 → callerTurn 应 = 1 (m1 序号).
    text_msg = json.dumps({"type": "assistant", "timestamp": "t0", "sessionId": "s-x",
                           "message": {"role": "assistant", "id": "m0",
                                       "content": [{"type": "text", "text": "hi"}]}})
    with open(p, "w") as f:
        f.write(text_msg + "\n" + agent_call_lines("a1", "Explore", in_msg_id="m1") + "\n")

    # depth-2: 作 root 文件解析 (caller=root: agentId=None, isRoot=True)
    recs, _ = parse_transcript_file(p, None, True, "s-x", "proj", {})
    subs = [r for r in recs if r.get("recordType") == "SubagentCall"]
    check("SubagentCall=1 (Agent tur 产 record)", len(subs) == 1, len(subs))
    check("callerTurn=1 (绑定含 Agent tool_use 的 m1, 非时序 m0)",
          subs and subs[0].get("callerTurn") == 1, subs and subs[0].get("callerTurn"))
    check("caller.agentId=None (root 文件归属 = depth-2)",
          subs and subs[0]["caller"]["agentId"] is None, subs and subs[0].get("caller"))
    check("caller.isRoot=True (root 文件)", subs and subs[0]["caller"]["isRoot"] is True,
          subs and subs[0]["caller"].get("isRoot"))

    # depth-3 plumbing: 同文件作父 spawn 解析 (caller=父 spawn "parent-spawn") → callerTurn 同值 (文件相对),
    # callerAgentId=父 spawn (非 root). 证明嵌套: 锚点进父 spawn 详情 而非 root.
    recs2, _ = parse_transcript_file(p, "parent-spawn", False, "s-x", "proj", {})
    subs2 = [r for r in recs2 if r.get("recordType") == "SubagentCall"]
    check("depth-3 callerTurn 同值=1 (文件相对, 与 root 解析一致)",
          subs2 and subs2[0].get("callerTurn") == 1, subs2 and subs2[0].get("callerTurn"))
    check("depth-3 caller.agentId=父 spawn (非 root, 嵌套)",
          subs2 and subs2[0]["caller"]["agentId"] == "parent-spawn",
          subs2 and subs2[0]["caller"].get("agentId"))
    check("depth-3 caller.isRoot=False (父 spawn 文件)",
          subs2 and subs2[0]["caller"]["isRoot"] is False,
          subs2 and subs2[0]["caller"].get("isRoot"))
    shutil.rmtree(tmp, ignore_errors=True)


test_subagent_call_caller_turn()

# ===== 收尾 =====
print("\n" + "=" * 70)
print(f"结果: {passed} PASS / {failed} FAIL")
print("验证范围: Mode B transcript ingest → §6 record → 复用 to_event/拓扑/自洽 (隔离合成).")
print("平台边界 (§9.3): Mode B 只重建 depth-2; depth-3 须 live hook (真 CC transcript 零嵌套).")
print("=" * 70)
sys.exit(1 if failed else 0)
