#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent-insight recorder 合成 stdin 单测 (Phase 1).

【隔离保证】(沿用 /tmp/cont-probe 打法, §13 续接原型同款)
  - 子进程用最小隔离 env (只 AGENTINSIGHT_LOG_DIR + PATH + 显式 carrier), 不继承当前 session env;
  - logDir 指向 tempfile.mkdtemp, 不碰真 ~/.claude / 真 CLAUDE_PLUGIN_DATA;
  - 不注册真 hook、不碰 settings.json / marketplace.json、不触发真 CC 事件.
  => 对当前 session 零影响.

测的是 recorder 的落盘逻辑 (三轨 record 构造 / effective_id / 滚动文件名 / Bash opt-in / 不阻断).
未覆盖 (留给 New Session live 验收, §13 红线): CC 是否真按 documented 触发 PostToolUse + 透传 payload 字段.
"""
import json
import os
import sys
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
RECORD = os.path.join(HERE, "..", "hooks", "record.py")
_TZ = timezone(timedelta(hours=8))

passed = failed = 0
TMP = None  # 本轮隔离 logDir


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}   {detail}")


def run_record(payload, carriers=None, extra=None):
    """隔离 env 调 record.py (logDir=TMP). 返回 exit code."""
    env = {"AGENTINSIGHT_LOG_DIR": TMP, "PATH": os.environ.get("PATH", "")}
    if carriers:
        env.update(carriers)
    if extra:
        env.update(extra)
    p = subprocess.run([sys.executable, RECORD], input=json.dumps(payload),
                       capture_output=True, text=True, env=env)
    return p.returncode


def read_jsonl():
    """读 TMP 下今天的 JSONL 全部行 (跨 project)."""
    today = datetime.now(_TZ).strftime("%Y-%m-%d")
    rows = []
    for proj in os.listdir(TMP):
        f = os.path.join(TMP, proj, today + ".jsonl")
        if os.path.exists(f):
            with open(f) as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        rows.append(json.loads(line))
    return rows


def read_lineage():
    """读 TMP/generations.jsonl (base 根 lineage 映射, §10.1; 非 project 子目录). 返回行列表."""
    path = os.path.join(TMP, "generations.jsonl")
    rows = []
    if os.path.exists(path):
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    return rows


def read_budget_events():
    """读 TMP/budget-events.jsonl (base 根, 外部动作层订阅面, §10). 返回行列表."""
    path = os.path.join(TMP, "budget-events.jsonl")
    rows = []
    if os.path.exists(path):
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    return rows


def run_record_proc(payload, carriers=None, extra=None):
    """隔离 env 调 record.py, 返回完整 process (含 .returncode/.stdout). 供 stdout 断言用 (组13)."""
    env = {"AGENTINSIGHT_LOG_DIR": TMP, "PATH": os.environ.get("PATH", "")}
    if carriers:
        env.update(carriers)
    if extra:
        env.update(extra)
    return subprocess.run([sys.executable, RECORD], input=json.dumps(payload),
                          capture_output=True, text=True, env=env)


def reset():
    global TMP
    if TMP and os.path.exists(TMP):
        shutil.rmtree(TMP)
    TMP = tempfile.mkdtemp(prefix="obs-test-")


print("=" * 70)
print("agent-insight recorder 合成 stdin 单测 (隔离, 不碰真 session)")
print("=" * 70)

# ===== 组1 Agent 轨道 (核心) =====
print("\n[组1] Agent 轨道 — SubagentCallRecord (子agent发起, 带 caller)")
reset()
run_record({
    "hook_event_name": "PostToolUse", "tool_name": "Agent",
    "session_id": "sess-1", "cwd": "/home/qwren/demo-project",
    "tool_use_id": "toolu_abc",
    "agent_id": "ad5a86caller", "agent_type": "demo-architect",  # F8 caller
    "tool_input": {"subagent_type": "Explore", "prompt": "..."},
    "tool_response": {
        "status": "completed", "agentId": "a8e63950cc3ce87d0", "agentType": "general-purpose",
        "resolvedModel": "glm-5.1", "totalDurationMs": 6386, "totalTokens": 9617,
        "usage": {"input_tokens": 6671, "output_tokens": 2,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 2944},
    },
})
rows = read_jsonl()
check("落盘 1 条", len(rows) == 1, len(rows))
r = rows[0] if rows else {}
check("recordType=SubagentCall", r.get("recordType") == "SubagentCall", r.get("recordType"))
check("subagentType=Explore (动态 key)", r.get("subagentType") == "Explore", r.get("subagentType"))
check("caller.isRoot=False (子agent发起)", r.get("caller", {}).get("isRoot") is False, r.get("caller"))
check("caller.agentId=caller id", r.get("caller", {}).get("agentId") == "ad5a86caller", r.get("caller"))
check("spawned.agentId", r.get("spawned", {}).get("agentId") == "a8e63950cc3ce87d0", r.get("spawned"))
tok = r.get("tokens", {})
check("tokens.input=6671", tok.get("input") == 6671, tok)
check("tokens.total=9617", tok.get("total") == 9617, tok)
check("durationMs=6386 (totalDurationMs)", r.get("durationMs") == 6386, r.get("durationMs"))
check("resolvedModel=glm-5.1 (F6)", r.get("resolvedModel") == "glm-5.1", r.get("resolvedModel"))
check("success=True (completed)", r.get("success") is True, r.get("success"))
check("error=None", r.get("error") is None, r.get("error"))
check("projectName=demo-project (cwd basename)", r.get("projectName") == "demo-project", r.get("projectName"))

# ===== 组2 Agent root 直发 (caller 缺失 → isRoot) =====
print("\n[组2] Agent root 直发 — caller 缺失 → isRoot=True")
reset()
run_record({
    "tool_name": "Agent", "session_id": "sess-2", "cwd": "/proj/x",
    "tool_input": {"subagent_type": "developer"},
    "tool_response": {"status": "completed", "agentId": "dev-1", "agentType": "developer",
                      "resolvedModel": "glm-5.1", "totalDurationMs": 100, "totalTokens": 500,
                      "usage": {"input_tokens": 400, "output_tokens": 100}},
})
r = read_jsonl()[0]
check("caller.isRoot=True (无顶层 agent_id)", r.get("caller", {}).get("isRoot") is True, r.get("caller"))
check("caller.agentId=None", r.get("caller", {}).get("agentId") is None, r.get("caller"))

# ===== 组3 effective_id (carrier 双通路 + inert) =====
print("\n[组3] effective_id — env carrier / handoff 文件 / inert fallback")
reset()
# 3a env carrier
run_record({"tool_name": "Agent", "session_id": "s-env", "cwd": "/p",
            "tool_input": {"subagent_type": "x"},
            "tool_response": {"status": "completed", "agentId": "a", "agentType": "x",
                              "totalTokens": 1, "usage": {}}},
           carriers={"AGENTINSIGHT_CARRIER_ID": "g-env"})
r = read_jsonl()[-1]
check("env carrier: generationId=g-env", r.get("generationId") == "g-env", r.get("generationId"))
check("env carrier: carrierSource=env", r.get("carrierSource") == "env", r.get("carrierSource"))
# 3b handoff 文件 carrier
hf = os.path.join(TMP, "handoff.json")
with open(hf, "w") as f:
    json.dump({"generationId": "g-file", "other": "..."}, f)
run_record({"tool_name": "Agent", "session_id": "s-file", "cwd": "/p",
            "tool_input": {"subagent_type": "x"},
            "tool_response": {"status": "completed", "agentId": "b", "agentType": "x",
                              "totalTokens": 1, "usage": {}}},
           carriers={"AGENTINSIGHT_CARRIER_FILE": hf})
r = read_jsonl()[-1]
check("handoff carrier: generationId=g-file", r.get("generationId") == "g-file", r.get("generationId"))
check("handoff carrier: carrierSource=handoff-file", r.get("carrierSource") == "handoff-file", r.get("carrierSource"))
# 3c inert (无 carrier)
run_record({"tool_name": "Agent", "session_id": "s-solo", "cwd": "/p",
            "tool_input": {"subagent_type": "x"},
            "tool_response": {"status": "completed", "agentId": "c", "agentType": "x",
                              "totalTokens": 1, "usage": {}}})
r = read_jsonl()[-1]
check("inert: generationId=sessionId", r.get("generationId") == "s-solo", r.get("generationId"))
check("inert: carrierSource=None", r.get("carrierSource") is None, r.get("carrierSource"))

# ===== 组4 Skill 轨道 (零 token) =====
print("\n[组4] Skill 轨道 — SkillCallRecord (零 token, F3)")
reset()
run_record({"tool_name": "Skill", "session_id": "sess-s", "cwd": "/p",
            "agent_id": "architect-1",
            "tool_input": {"skill": "executing-plans"},
            "tool_response": {"success": True, "commandName": "superpowers:executing-plans"}})
r = read_jsonl()[0]
check("recordType=SkillCall", r.get("recordType") == "SkillCall", r.get("recordType"))
check("skillName=superpowers:executing-plans (commandName)", r.get("skillName") == "superpowers:executing-plans", r.get("skillName"))
check("tokens=None (零 token F3)", r.get("tokens") is None, r.get("tokens"))
check("success=True", r.get("success") is True, r.get("success"))
check("caller 仍带 (architect-1)", r.get("caller", {}).get("agentId") == "architect-1", r.get("caller"))

# ===== 组5 Bash 轨道 (opt-in gate) =====
print("\n[组5] Bash 轨道 — opt-in gate (默认关, AGENTINSIGHT_BASH=1 才记)")
reset()
# 5a 默认关
rc = run_record({"tool_name": "Bash", "session_id": "sess-b", "cwd": "/p",
                 "tool_input": {"command": "pytest"},
                 "tool_response": {"stdout": "", "stderr": "1 failed", "interrupted": False}})
check("默认无 env: 不落盘 (no-op)", len(read_jsonl()) == 0, len(read_jsonl()))
check("默认无 env: exit 0 (不阻断)", rc == 0, rc)
# 5b opt-in 开
run_record({"tool_name": "Bash", "session_id": "sess-b", "cwd": "/p",
            "tool_input": {"command": "pytest"},
            "tool_response": {"stdout": "", "stderr": "1 failed", "interrupted": False}},
           extra={"AGENTINSIGHT_BASH": "1"})
r = read_jsonl()[0]
check("opt-in: recordType=Command", r.get("recordType") == "Command", r.get("recordType"))
check("opt-in: exitCode=None (F5 无 exit code)", r.get("exitCode") is None, r.get("exitCode"))
check("opt-in: interrupted=False", r.get("interrupted") is False, r.get("interrupted"))
check("opt-in: stderr 记下", r.get("stderr") == "1 failed", r.get("stderr"))
check("opt-in: command 记下", r.get("command") == "pytest", r.get("command"))

# ===== 组6 滚动文件名 + project 子目录 =====
print("\n[组6] 滚动文件名 (按天) + project 子目录")
reset()
run_record({"tool_name": "Agent", "session_id": "s", "cwd": "/home/qwren/myproj",
            "tool_input": {"subagent_type": "x"},
            "tool_response": {"status": "completed", "agentId": "a", "agentType": "x",
                              "totalTokens": 1, "usage": {}}})
today = datetime.now(_TZ).strftime("%Y-%m-%d")
expected = os.path.join(TMP, "myproj", today + ".jsonl")
check("文件名=YYYY-MM-DD.jsonl", os.path.exists(expected), expected)
check("project 子目录名 = cwd basename", os.path.isdir(os.path.join(TMP, "myproj")), TMP)

# ===== 组7 不阻断 (异常 swallow) =====
print("\n[组7] 不阻断 — 烂 stdin / 缺字段 → exit 0, 不崩")
reset()
# 7a 烂 stdin
p = subprocess.run([sys.executable, RECORD], input="not json",
                   capture_output=True, text=True,
                   env={"AGENTINSIGHT_LOG_DIR": TMP, "PATH": os.environ.get("PATH", "")})
check("烂 stdin: exit 0", p.returncode == 0, p.returncode)
check("烂 stdin: 无落盘", len(read_jsonl()) == 0, len(read_jsonl()))
# 7b 缺关键字段 (无 tool_response) — 仍不崩
rc = run_record({"tool_name": "Agent", "session_id": "s"})
check("缺字段: exit 0 (不阻断)", rc == 0, rc)

# ===== 组8 非三轨 tool_name → no-op =====
print("\n[组8] 非三轨 tool_name → no-op")
reset()
rc = run_record({"tool_name": "Read", "session_id": "s", "cwd": "/p"})
check("Read: 不落盘", len(read_jsonl()) == 0, len(read_jsonl()))
check("Read: exit 0", rc == 0, rc)

# ===== 组9 SessionStart — carrier 三路 (env / handoff / inert) =====
print("\n[组9] SessionStart lineage — carrier 三路 (env / handoff-file / inert fallback)")
reset()
# 9a env carrier
rc = run_record({"hook_event_name": "SessionStart", "session_id": "s1",
                 "cwd": "/home/qwren/demo", "source": "startup"},
                carriers={"AGENTINSIGHT_CARRIER_ID": "g-env"})
check("SessionStart env: exit 0", rc == 0, rc)
check("SessionStart env: per-event log 无污染 (无 SubagentCall)", len(read_jsonl()) == 0, len(read_jsonl()))
rows = read_lineage()
check("SessionStart env: generations.jsonl 1 行", len(rows) == 1, len(rows))
r = rows[0] if rows else {}
check("SessionStart env: recordType=GenerationLineage", r.get("recordType") == "GenerationLineage", r.get("recordType"))
check("SessionStart env: generationId=g-env", r.get("generationId") == "g-env", r.get("generationId"))
check("SessionStart env: sessionId=s1", r.get("sessionId") == "s1", r.get("sessionId"))
check("SessionStart env: carrierSource=env", r.get("carrierSource") == "env", r.get("carrierSource"))
check("SessionStart env: source=startup", r.get("source") == "startup", r.get("source"))
check("SessionStart env: writer=plugin-hook", r.get("writer") == "plugin-hook", r.get("writer"))
check("SessionStart env: projectName=demo (cwd basename)", r.get("projectName") == "demo", r.get("projectName"))
# 9b handoff 文件 carrier
reset()
hf = os.path.join(TMP, "handoff.json")
with open(hf, "w") as f:
    json.dump({"generationId": "g-file", "other": "..."}, f)
run_record({"hook_event_name": "SessionStart", "session_id": "s2", "cwd": "/p", "source": "resume"},
           carriers={"AGENTINSIGHT_CARRIER_FILE": hf})
r = read_lineage()[0]
check("SessionStart handoff: generationId=g-file", r.get("generationId") == "g-file", r.get("generationId"))
check("SessionStart handoff: carrierSource=handoff-file", r.get("carrierSource") == "handoff-file", r.get("carrierSource"))
check("SessionStart handoff: source=resume", r.get("source") == "resume", r.get("source"))
# 9c inert (无 carrier)
reset()
run_record({"hook_event_name": "SessionStart", "session_id": "s-solo", "cwd": "/p"})
r = read_lineage()[0]
check("SessionStart inert: generationId=sessionId", r.get("generationId") == "s-solo", r.get("generationId"))
check("SessionStart inert: carrierSource=null", r.get("carrierSource") is None, r.get("carrierSource"))
check("SessionStart inert: source=null (payload 无 source)", r.get("source") is None, r.get("source"))

# ===== 组10 SessionStart 4 源全 fire =====
print("\n[组10] SessionStart 4 源全 fire (startup/resume/clear/compact)")
reset()
for src in ["startup", "resume", "clear", "compact"]:
    run_record({"hook_event_name": "SessionStart", "session_id": f"s-{src}",
                "cwd": "/p", "source": src})
rows = read_lineage()
check("4 源各写 1 行 (共 4)", len(rows) == 4, len(rows))
got_sources = sorted(r.get("source") for r in rows)
check("4 源 source 字段全记对", got_sources == ["clear", "compact", "resume", "startup"], got_sources)

# ===== 组11 env carrier 优先 handoff 文件 =====
print("\n[组11] SessionStart — env carrier 优先 handoff 文件 (read_carrier 复用)")
reset()
hf = os.path.join(TMP, "handoff.json")
with open(hf, "w") as f:
    json.dump({"generationId": "g-file"}, f)
run_record({"hook_event_name": "SessionStart", "session_id": "s-both", "cwd": "/p", "source": "startup"},
           carriers={"AGENTINSIGHT_CARRIER_ID": "g-env", "AGENTINSIGHT_CARRIER_FILE": hf})
r = read_lineage()[0]
check("env+handoff 同设: env 赢 (generationId=g-env)", r.get("generationId") == "g-env", r.get("generationId"))
check("env+handoff 同设: carrierSource=env", r.get("carrierSource") == "env", r.get("carrierSource"))

# ===== 组12 generations.jsonl 位置 + append (base 根, 非 project 子目录) =====
print("\n[组12] generations.jsonl — base 根放置 + 连发 append (异 session 同 carrier)")
reset()
run_record({"hook_event_name": "SessionStart", "session_id": "sess-A", "cwd": "/projA", "source": "startup"},
           carriers={"AGENTINSIGHT_CARRIER_ID": "g-shared"})
run_record({"hook_event_name": "SessionStart", "session_id": "sess-B", "cwd": "/projB", "source": "startup"},
           carriers={"AGENTINSIGHT_CARRIER_ID": "g-shared"})
rows = read_lineage()
check("连发两 SessionStart: generations.jsonl 2 行", len(rows) == 2, len(rows))
check("两行同 carrier (缝合前提)", all(r.get("generationId") == "g-shared" for r in rows),
      [r.get("generationId") for r in rows])
check("两行 sessionId 各异 (sess-A/sess-B)", sorted(r.get("sessionId") for r in rows) == ["sess-A", "sess-B"],
      [r.get("sessionId") for r in rows])
check("base 根放置: <TMP>/generations.jsonl 存在", os.path.exists(os.path.join(TMP, "generations.jsonl")))
check("非 project 子目录: <TMP>/projA/generations.jsonl 不存在 (lineage 不按 project 分)",
      not os.path.exists(os.path.join(TMP, "projA", "generations.jsonl")))
check("lineage 不污染 per-event log (两 SessionStart 仍无 SubagentCall)", len(read_jsonl()) == 0, len(read_jsonl()))

# ===== 组13 SessionStart 红线 (不阻断 + 不注 additionalContext) =====
print("\n[组13] SessionStart 红线 — 烂 stdin/缺字段 exit 0; stdout 空 (不注 additionalContext)")
reset()
# 13a 烂 stdin → 第一道 try 拦截 → exit 0, 无 lineage
p = subprocess.run([sys.executable, RECORD], input="not json",
                   capture_output=True, text=True,
                   env={"AGENTINSIGHT_LOG_DIR": TMP, "PATH": os.environ.get("PATH", "")})
check("烂 stdin: exit 0", p.returncode == 0, p.returncode)
check("烂 stdin: 无 lineage 落盘", len(read_lineage()) == 0, len(read_lineage()))
# 13b 缺 session_id → 不崩, 退化 unknown-session, exit 0
rc = run_record({"hook_event_name": "SessionStart", "cwd": "/p", "source": "startup"})
check("缺 session_id: exit 0 (不阻断)", rc == 0, rc)
r = read_lineage()[0]
check("缺 session_id: sessionId=unknown-session", r.get("sessionId") == "unknown-session", r.get("sessionId"))
# 13c stdout 空 (observe-only 红线 2: 不向 Claude 注 additionalContext)
reset()
p = run_record_proc({"hook_event_name": "SessionStart", "session_id": "s-stdout", "cwd": "/p", "source": "startup"})
check("SessionStart: stdout 空 (不注 additionalContext, 红线 2)", p.stdout.strip() == "", repr(p.stdout))
check("SessionStart: exit 0", p.returncode == 0, p.returncode)
check("SessionStart: lineage 仍落 (副作用生效)", len(read_lineage()) == 1, len(read_lineage()))

# ===== 组14 实时预算事件 emission (opt-in §10, 2026-06-24) =====
print("\n[组14] 实时预算事件 emission — opt-in (threshold 配了才发) + 仅 Agent 轨")
reset()
# 14a 没配 threshold → 不发 emission (inert, 红线 2)
run_record({"tool_name": "Agent", "session_id": "s-bud", "cwd": "/p",
            "tool_input": {"subagent_type": "x"},
            "tool_response": {"status": "completed", "agentId": "a", "agentType": "x",
                              "totalTokens": 500, "usage": {"input_tokens": 400, "output_tokens": 100}}})
check("14a 没配 threshold: budget-events.jsonl 不存在 (opt-in)",
      not os.path.exists(os.path.join(TMP, "budget-events.jsonl")))
# 14b 配 threshold + Agent → 落 BudgetEvent
reset()
run_record({"tool_name": "Agent", "session_id": "s-bud", "cwd": "/p",
            "tool_input": {"subagent_type": "x"},
            "tool_response": {"status": "completed", "agentId": "a", "agentType": "x",
                              "totalTokens": 500, "usage": {"input_tokens": 400, "output_tokens": 100}}},
           extra={"AGENTINSIGHT_BUDGET_THRESHOLD": "1000"})
evs = read_budget_events()
check("14b 配 threshold: budget-events.jsonl 1 条", len(evs) == 1, len(evs))
e = evs[0] if evs else {}
check("14b recordType=BudgetEvent", e.get("recordType") == "BudgetEvent", e.get("recordType"))
check("14b cumulativeTotal=500 (本 session 累计)", e.get("cumulativeTotal") == 500, e.get("cumulativeTotal"))
check("14b threshold=1000", e.get("threshold") == 1000, e.get("threshold"))
check("14b pctOfThreshold=50.0", e.get("pctOfThreshold") == 50.0, e.get("pctOfThreshold"))
check("14b exceeded=False (500<1000)", e.get("exceeded") is False, e.get("exceeded"))
check("14b sessionId=s-bud", e.get("sessionId") == "s-bud", e.get("sessionId"))
check("14b tokens 四桶带回 (input=400)", e.get("tokens", {}).get("input") == 400, e.get("tokens"))
# 14c 第二条 Agent → 累计翻倍 + 到阈即超 (per-session 实时累计)
run_record({"tool_name": "Agent", "session_id": "s-bud", "cwd": "/p",
            "tool_input": {"subagent_type": "x"},
            "tool_response": {"status": "completed", "agentId": "b", "agentType": "x",
                              "totalTokens": 500, "usage": {"input_tokens": 400, "output_tokens": 100}}},
           extra={"AGENTINSIGHT_BUDGET_THRESHOLD": "1000"})
evs = read_budget_events()
check("14c 第二条 Agent 后: budget-events 共 2 条 (每 Agent 触发一次)", len(evs) == 2, len(evs))
e2 = evs[-1] if evs else {}
check("14c 第二条 cumulativeTotal=1000 (累计翻倍)", e2.get("cumulativeTotal") == 1000, e2.get("cumulativeTotal"))
check("14c 第二条 exceeded=True (到阈即超 1000>=1000)", e2.get("exceeded") is True, e2.get("exceeded"))
check("14c 第二条 pctOfThreshold=100.0", e2.get("pctOfThreshold") == 100.0, e2.get("pctOfThreshold"))
# 14d Skill 轨不发 emission (token 在 Agent 轨)
reset()
run_record({"tool_name": "Skill", "session_id": "s-skill", "cwd": "/p",
            "agent_id": "arch", "tool_input": {"skill": "x"},
            "tool_response": {"success": True, "commandName": "x"}},
           extra={"AGENTINSIGHT_BUDGET_THRESHOLD": "1000"})
check("14d Skill 轨: 不发 emission (budget-events 不存在)",
      not os.path.exists(os.path.join(TMP, "budget-events.jsonl")))
# 14e emission 不阻断编排: budget 模块炸了也不影响落盘 (红线 1 — 难造, 跳过; 已由 try/except 兜)

# ===== 收尾 =====
print("\n" + "=" * 70)
print(f"结果: {passed} PASS / {failed} FAIL")
print("验证范围: recorder 落盘逻辑 (三轨 record / effective_id / 滚动文件名 / Bash opt-in / 不阻断).")
print("未覆盖 (留给 New Session live 验收): CC 真触发 PostToolUse + payload 字段透传.")
print("=" * 70)
if TMP and os.path.exists(TMP):
    shutil.rmtree(TMP)
sys.exit(1 if failed else 0)
