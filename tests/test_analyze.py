#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent-insight offline reader (tools/analyze.py, Mode A) 单测.

【隔离保证】(同 test_record.py 打法)
  - 合成 §6 形状的 record 写进 tempfile, 子进程 `analyze.py --jsonl <tmp> --json` 跑;
  - 不碰真 ~/.claude / 真 logdir / 真 session / settings.json / marketplace.json.
  => 对当前 session 零影响.

测的是 reader 的 IR 重建 (§9.1) + §7 路径A 拓扑 (agent_id 离线链接) + 自洽诊断 (§9.4):
  root 直发 / depth-3 嵌套 / 并行多波 / orphan / 多 session agentId 碰撞不串链 /
  isRoot 不变量违例 / Skill+Command 计数不入 token 账 / 空文件 / token 聚合 / --logdir 目录扫描.
Mode B (喂 CC transcript, §9.2·B) 已交付 tools/transcript_adapter.py + analyze.py --transcript,
独立单测 tests/test_transcript_adapter.py 覆盖 (45/45); 本文件只测 Mode A 自有 JSONL 通路.
"""
import json
import os
import sys
import shutil
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ANALYZE = os.path.join(HERE, "..", "tools", "analyze.py")

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}   {detail}")


# ---------- 合成 §6 record 构造器 ----------
def subagent(session_id, subagent_type, spawned_id, caller_id=None, caller_type=None,
             total=100, inp=80, out=20, ccr=0, cr=0, dur=1000,
             ts="2026-06-16T13:00:00+08:00", model="glm-5.1", success=True,
             is_root=None, tool_use_id="tu"):
    """合成 SubagentCallRecord. is_root 默认由 caller_id 推 (与 record.py:122 同源)."""
    if is_root is None:
        is_root = (caller_id is None)
    return {
        "schemaVersion": 1, "timestamp": ts,
        "runId": session_id, "generationId": session_id, "carrierSource": None,
        "projectName": "test", "sessionId": session_id, "toolUseId": tool_use_id,
        "caller": {"agentId": caller_id, "agentType": caller_type, "isRoot": is_root},
        "budgetState": None,
        "recordType": "SubagentCall", "subagentType": subagent_type,
        "spawned": {"agentId": spawned_id, "agentType": subagent_type},
        "tokens": {"input": inp, "output": out, "cacheCreation": ccr, "cacheRead": cr, "total": total},
        "durationMs": dur, "resolvedModel": model,
        "success": success, "error": None if success else "failed",
    }


def skill_call(session_id, skill_name, caller_id=None):
    return {
        "schemaVersion": 1, "timestamp": "2026-06-16T13:00:00+08:00",
        "runId": session_id, "generationId": session_id, "carrierSource": None,
        "projectName": "test", "sessionId": session_id, "toolUseId": "tu-s",
        "caller": {"agentId": caller_id, "agentType": None, "isRoot": caller_id is None},
        "budgetState": None,
        "recordType": "SkillCall", "skillName": skill_name, "success": True, "tokens": None,
    }


def command_rec(session_id, cmd, caller_id=None):
    return {
        "schemaVersion": 1, "timestamp": "2026-06-16T13:00:00+08:00",
        "runId": session_id, "generationId": session_id, "carrierSource": None,
        "projectName": "test", "sessionId": session_id, "toolUseId": "tu-b",
        "caller": {"agentId": caller_id, "agentType": None, "isRoot": caller_id is None},
        "budgetState": None,
        "recordType": "Command", "command": cmd, "interrupted": False, "stderr": "", "exitCode": None,
    }


def run_jsonl(records, extra=None):
    """合成 records → tempfile → analyze.py --jsonl <tmp> --json. 返回解析后的 dict."""
    d = tempfile.mkdtemp(prefix="obs-an-")
    fp = os.path.join(d, "2026-06-16.jsonl")
    with open(fp, "w") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    cmd = [sys.executable, ANALYZE, "--jsonl", fp, "--json"]
    if extra:
        cmd += extra
    p = subprocess.run(cmd, capture_output=True, text=True)
    shutil.rmtree(d, ignore_errors=True)
    if p.returncode != 0:
        return {"_rc": p.returncode, "_stderr": p.stderr, "_stdout": p.stdout}
    try:
        return json.loads(p.stdout)
    except Exception:
        return {"_rc": p.returncode, "_stderr": p.stderr, "_stdout": p.stdout}


def run_logdir(records_by_project):
    """合成 <logdir>/<project>/2026-06-16.jsonl → analyze.py --logdir <logdir> --json."""
    d = tempfile.mkdtemp(prefix="obs-an-ld-")
    for proj, recs in records_by_project.items():
        os.makedirs(os.path.join(d, proj), exist_ok=True)
        with open(os.path.join(d, proj, "2026-06-16.jsonl"), "w") as f:
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    p = subprocess.run([sys.executable, ANALYZE, "--logdir", d, "--json"],
                       capture_output=True, text=True)
    shutil.rmtree(d, ignore_errors=True)
    if p.returncode != 0:
        return {"_rc": p.returncode, "_stderr": p.stderr}
    try:
        return json.loads(p.stdout)
    except Exception:
        return {"_rc": p.returncode, "_stderr": p.stderr}


def find_chain(res, subagent_type, session_id=None):
    """从 callChains 找一条匹配 subagentType(可选 session)的派生记录."""
    for c in res.get("callChains", []):
        if c.get("subagentType") == subagent_type:
            if session_id is None or c.get("sessionId") == session_id:
                return c
    return None


print("=" * 70)
print("agent-insight offline reader 单测 (Mode A · own-JSONL, 隔离)")
print("=" * 70)

# ===== 组1 root 直发 (单条, 同 live 样本形态) =====
print("\n[组1] root 直发 — caller 缺失 → isRoot, chain 前置 orchestrator")
res = run_jsonl([subagent("s1", "general-purpose", "A1", total=12805, inp=11843, out=2, cr=960, dur=6610)])
check("落盘 1 条解析", res.get("recordsTotal") == 1, res)
check("byTrack SubagentCall=1", res.get("byTrack", {}).get("SubagentCall") == 1, res.get("byTrack"))
check("sessions=1", len(res.get("sessions", [])) == 1, res.get("sessions"))
check("grandTotal.total=12805", res.get("grandTotal", {}).get("total") == 12805, res.get("grandTotal"))
c = find_chain(res, "general-purpose")
check("chain=[orchestrator, general-purpose]", c and c.get("callChain") == ["orchestrator", "general-purpose"], c)
check("parentType=orchestrator", c and c.get("parentType") == "orchestrator", c)
check("trigger=root", c and c.get("trigger") == "root", c)
check("depth=2", c and c.get("depth") == 2, c)
check("orphan=False (达根)", c and c.get("orphan") is False, c)
check("consistent=True", res.get("consistency", {}).get("consistent") is True, res.get("consistency"))

# ===== 组2 depth-3 嵌套 (root→architect→Explore, §7 现役路径) =====
print("\n[组2] depth-3 嵌套 — root→architect→Explore (agent_id 离线链接)")
res = run_jsonl([
    subagent("s2", "architect", spawned_id="A1", caller_id=None, ts="2026-06-16T13:00:01+08:00", total=500),
    subagent("s2", "Explore", spawned_id="E1", caller_id="A1", caller_type="architect", ts="2026-06-16T13:00:02+08:00", total=300),
])
c_arch = find_chain(res, "architect")
c_exp = find_chain(res, "Explore")
check("architect chain=[orchestrator, architect]", c_arch and c_arch.get("callChain") == ["orchestrator", "architect"], c_arch)
check("architect depth=2", c_arch and c_arch.get("depth") == 2, c_arch)
check("Explore chain=[orchestrator, architect, Explore]", c_exp and c_exp.get("callChain") == ["orchestrator", "architect", "Explore"], c_exp)
check("Explore parentType=architect", c_exp and c_exp.get("parentType") == "architect", c_exp)
check("Explore trigger=subagent", c_exp and c_exp.get("trigger") == "subagent", c_exp)
check("Explore depth=3", c_exp and c_exp.get("depth") == 3, c_exp)
# call graph: orchestrator→architect x1, architect→Explore x1
edges = {(e["parentType"], e["childType"]): e["count"] for e in res.get("callGraph", [])}
check("call graph: orchestrator→architect x1", edges.get(("orchestrator", "architect")) == 1, edges)
check("call graph: architect→Explore x1", edges.get(("architect", "Explore")) == 1, edges)
check("grandTotal=800 (500+300)", res.get("grandTotal", {}).get("total") == 800, res.get("grandTotal"))

# ===== 组3 并行多波 (root→reviewer→{cs1, cs2}, §7 reviewer 高频路径, F8) =====
print("\n[组3] 并行多波 — root→reviewer→2×code-summarizer (不串链)")
res = run_jsonl([
    subagent("s3", "reviewer", spawned_id="RV1", caller_id=None, ts="2026-06-16T13:00:01+08:00", total=400),
    subagent("s3", "code-summarizer", spawned_id="CS1", caller_id="RV1", ts="2026-06-16T13:00:02+08:00", total=200),
    subagent("s3", "code-summarizer", spawned_id="CS2", caller_id="RV1", ts="2026-06-16T13:00:03+08:00", total=250),
])
cs_chains = [c for c in res.get("callChains", []) if c.get("subagentType") == "code-summarizer"]
check("2 条 code-summarizer 派生记录", len(cs_chains) == 2, len(cs_chains))
check("两条 cs 都 parentType=reviewer", all(c.get("parentType") == "reviewer" for c in cs_chains), cs_chains)
check("两条 cs 都 depth=3", all(c.get("depth") == 3 for c in cs_chains), cs_chains)
check("两条 cs spawned id 不同 (CS1/CS2)", {c.get("spawnedAgentId") for c in cs_chains} == {"CS1", "CS2"}, cs_chains)
edges = {(e["parentType"], e["childType"]): e["count"] for e in res.get("callGraph", [])}
check("call graph: reviewer→code-summarizer x2 (多波聚合)", edges.get(("reviewer", "code-summarizer")) == 2, edges)

# ===== 组4 orphan (caller 在本 session 未被 spawned → 未达根, §9.3 caveat) =====
print("\n[组4] orphan — caller=GHOST 未被捕获, 链中途断 (非一致性违例)")
res = run_jsonl([subagent("s4", "Explore", spawned_id="E1", caller_id="GHOST", total=100)])
c = find_chain(res, "Explore")
check("orphan=True", c and c.get("orphan") is True, c)
check("chain 不含 orchestrator 前缀", c and c.get("callChain") == ["Explore"], c)
check("parentType=None (未解析)", c and c.get("parentType") is None, c)
check("orphanChains=1", res.get("consistency", {}).get("orphanChains") == 1, res.get("consistency"))
check("GHOST 在 orphanCallerIds", "GHOST" in res.get("consistency", {}).get("orphanCallerIds", []), res.get("consistency"))
check("consistent 仍 True (orphan 非违例)", res.get("consistency", {}).get("consistent") is True, res.get("consistency"))

# ===== 组5 多 session agentId 碰撞不串链 (session 内作用域, §10.1) =====
print("\n[组5] 多 session agentId 碰撞 — 按 sessionId 分组, 不跨 session 串链")
res = run_jsonl([
    subagent("sa", "architect", spawned_id="X1", caller_id=None, ts="2026-06-16T13:00:01+08:00", total=100),
    subagent("sa", "Explore", spawned_id="X2", caller_id="X1", ts="2026-06-16T13:00:02+08:00", total=50),
    subagent("sb", "developer", spawned_id="X1", caller_id=None, ts="2026-06-16T13:00:03+08:00", total=100),  # X1 碰撞!
    subagent("sb", "tester", spawned_id="X3", caller_id="X1", ts="2026-06-16T13:00:04+08:00", total=50),
])
check("sessions=2 (sa, sb)", set(res.get("sessions", [])) == {"sa", "sb"}, res.get("sessions"))
c_exp = find_chain(res, "Explore", "sa")
c_tst = find_chain(res, "tester", "sb")
check("sa: Explore parentType=architect (本 session 解析)", c_exp and c_exp.get("parentType") == "architect", c_exp)
check("sa: Explore chain=[orchestrator, architect, Explore]", c_exp and c_exp.get("callChain") == ["orchestrator", "architect", "Explore"], c_exp)
check("sb: tester parentType=developer (不被 sa 的 X1 串)", c_tst and c_tst.get("parentType") == "developer", c_tst)
check("sb: tester chain=[orchestrator, developer, tester]", c_tst and c_tst.get("callChain") == ["orchestrator", "developer", "tester"], c_tst)
check("无 orphan (两 session 各自闭环)", res.get("consistency", {}).get("orphanChains") == 0, res.get("consistency"))

# ===== 组6 isRoot 不变量违例 (recorder bug 交叉校验, §9.4) =====
print("\n[组6] isRoot 不变量违例 — agentId=None 但 isRoot=False → consistent=False")
res = run_jsonl([subagent("s6", "x", spawned_id="A", caller_id=None, is_root=False)])
check("consistent=False (抓到违例)", res.get("consistency", {}).get("consistent") is False, res.get("consistency"))
check("isRootInvariantViolations 非空", len(res.get("consistency", {}).get("isRootInvariantViolations", [])) == 1, res.get("consistency"))

# ===== 组7 Skill/Command 计入 byTrack 但不入 token 账 / 拓扑 =====
print("\n[组7] Skill + Command — 计入 byTrack, 不计 token / 不入拓扑")
res = run_jsonl([
    subagent("s7", "developer", spawned_id="D1", total=500, inp=400, out=100),
    skill_call("s7", "superpowers:executing-plans"),
    command_rec("s7", "pytest"),
])
bt = res.get("byTrack", {})
check("byTrack SubagentCall=1", bt.get("SubagentCall") == 1, bt)
check("byTrack SkillCall=1", bt.get("SkillCall") == 1, bt)
check("byTrack Command=1", bt.get("Command") == 1, bt)
check("grandTotal 只算 SubagentCall=500", res.get("grandTotal", {}).get("total") == 500, res.get("grandTotal"))
check("callChains 只 1 条 (Skill/Command 不入拓扑)", len(res.get("callChains", [])) == 1, len(res.get("callChains", [])))

# ===== 组8 空文件 (graceful, 不崩) =====
print("\n[组8] 空文件 — recordsTotal=0, grandTotal 全 0, 不崩")
res = run_jsonl([])
check("recordsTotal=0", res.get("recordsTotal") == 0, res)
check("grandTotal.total=0", res.get("grandTotal", {}).get("total") == 0, res.get("grandTotal"))
check("callChains=[]", res.get("callChains") == [], res.get("callChains"))
check("consistent=True (无数据即无不变量违例)", res.get("consistency", {}).get("consistent") is True, res.get("consistency"))

# ===== 组9 --logdir 目录扫描 (多 project) =====
print("\n[组9] --logdir 目录扫描 — 跨 project 子目录聚合")
res = run_logdir({
    "proj-a": [subagent("sa", "architect", "A1", total=300)],
    "proj-b": [subagent("sb", "developer", "B1", total=700)],
})
check("跨 2 project 聚合 recordsTotal=2", res.get("recordsTotal") == 2, res)
check("grandTotal.total=1000 (300+700)", res.get("grandTotal", {}).get("total") == 1000, res.get("grandTotal"))
check("sessions=2", len(res.get("sessions", [])) == 2, res.get("sessions"))

# ===== 组10 --tree (人类输出) 不崩 + exit 0 =====
print("\n[组10] --tree 人类输出 — exit 0, 有 chain 行")
d = tempfile.mkdtemp(prefix="obs-an-tree-")
fp = os.path.join(d, "2026-06-16.jsonl")
with open(fp, "w") as f:
    f.write(json.dumps(subagent("s10", "general-purpose", "A1"), ensure_ascii=False) + "\n")
p = subprocess.run([sys.executable, ANALYZE, "--jsonl", fp, "--tree"], capture_output=True, text=True)
shutil.rmtree(d, ignore_errors=True)
check("--tree exit 0", p.returncode == 0, p.returncode)
check("--tree 含 'orchestrator → general-purpose'", "orchestrator → general-purpose" in p.stdout, p.stdout)
check("--tree 含 'self-consistency'", "self-consistency" in p.stdout, p.stdout)

# ===== 组11a · by_skill / _merge_by_skill 纯函数 (Plan D T1+T2: spawns/sessionIds/spawnIds) =====
sys.path.insert(0, os.path.join(os.path.dirname(ANALYZE)))
from analyze import by_skill, _merge_by_skill

print("\n[组11a] by_skill / _merge_by_skill —— spawns/sessionIds/spawnIds (Plan D)")
ev = [
    {"recordType": "SkillCall", "skillName": "brainstorming", "sessionId": "sessionA",
     "callerAgentId": None, "callerType": "orchestrator"},
    {"recordType": "SkillCall", "skillName": "brainstorming", "sessionId": "sessionA",
     "callerAgentId": "agent-X", "callerType": "Explore"},
    {"recordType": "SkillCall", "skillName": "brainstorming", "sessionId": "sessionB",
     "callerAgentId": "agent-Y", "callerType": "Plan"},
    {"recordType": "SkillCall", "skillName": "deep-research", "sessionId": "sessionA",
     "callerAgentId": None, "callerType": "orchestrator"},
]
bs = by_skill(ev)
br = next(r for r in bs if r["skillName"] == "brainstorming")
check("by_skill brainstorming calls=3", br["calls"] == 3, br["calls"])
check("by_skill brainstorming sessions=2", br["sessions"] == 2, br["sessions"])
check("by_skill brainstorming spawns=2 (root None 不计)", br["spawns"] == 2, br["spawns"])
check("by_skill brainstorming sessionIds sorted", br["sessionIds"] == ["sessionA", "sessionB"], br["sessionIds"])
check("by_skill brainstorming spawnIds sorted", br["spawnIds"] == ["agent-X", "agent-Y"], br["spawnIds"])
check("by_skill brainstorming callerTypes", br["callerTypes"] == {"orchestrator": 1, "Explore": 1, "Plan": 1}, br["callerTypes"])
dr = next(r for r in bs if r["skillName"] == "deep-research")
check("by_skill deep-research spawns=0 (纯 root)", dr["spawns"] == 0, dr["spawns"])
check("by_skill deep-research sessionIds", dr["sessionIds"] == ["sessionA"], dr["sessionIds"])
check("by_skill 行无 token 字段 (F3)", "input" not in br and "total" not in br, list(br.keys()))

# _merge_by_skill: 跨 session union (非加 len)
per_session = [
    [{"skillName": "brainstorming", "calls": 1, "sessions": 1, "spawns": 1,
      "sessionIds": ["session1"], "spawnIds": ["agent-X"], "callerTypes": {"Explore": 1}}],
    [{"skillName": "brainstorming", "calls": 1, "sessions": 1, "spawns": 1,
      "sessionIds": ["session2"], "spawnIds": ["agent-Y"], "callerTypes": {"Plan": 1}},
     {"skillName": "deep-research", "calls": 1, "sessions": 1, "spawns": 0,
      "sessionIds": ["session2"], "spawnIds": [], "callerTypes": {"orchestrator": 1}}],
]
mg = _merge_by_skill(per_session)
mbr = next(r for r in mg if r["skillName"] == "brainstorming")
check("merge brainstorming calls=2", mbr["calls"] == 2, mbr["calls"])
check("merge brainstorming sessions=2", mbr["sessions"] == 2, mbr["sessions"])
check("merge brainstorming spawns=2 (union 非加 len)", mbr["spawns"] == 2, mbr["spawns"])
check("merge brainstorming sessionIds union sorted", mbr["sessionIds"] == ["session1", "session2"], mbr["sessionIds"])
check("merge brainstorming spawnIds union sorted", mbr["spawnIds"] == ["agent-X", "agent-Y"], mbr["spawnIds"])
check("merge brainstorming callerTypes", mbr["callerTypes"] == {"Explore": 1, "Plan": 1}, mbr["callerTypes"])
mdr = next(r for r in mg if r["skillName"] == "deep-research")
check("merge deep-research spawns=0", mdr["spawns"] == 0, mdr["spawns"])
check("merge 降序 brainstorming 在前", mg[0]["skillName"] == "brainstorming", mg[0]["skillName"])

# ===== 组11 · by_skill 切面（§8.11 · 零 token F3）=====
print("\n[组11] by_skill —— 活跃度表 + caller 共现, 零 token")
res = run_jsonl([
    subagent("s11", "Explore", spawned_id="E1", total=300),
    skill_call("s11", "deep-research", caller_id="E1"),
    skill_call("s11", "deep-research", caller_id="E1"),   # 同 skill 两次 → calls=2
    skill_call("s11", "superpowers:brainstorming"),        # caller_id=None → root 直发
])
bs = res.get("bySkill", [])
check("bySkill 存在", isinstance(bs, list), bs)
check("bySkill 2 行 (两 skill)", len(bs) == 2, bs)
# 按 calls 降序: deep-research(2) 在前
top = bs[0]
check("top=deep-research", top["skillName"] == "deep-research", top)
check("deep-research calls=2", top["calls"] == 2, top)
check("deep-research sessions=1", top["sessions"] == 1, top)
check("deep-research callerTypes Explore×2 (spawned 映射解析)",
      top["callerTypes"] == {"Explore": 2}, top["callerTypes"])
root_skill = [r for r in bs if r["skillName"] == "superpowers:brainstorming"][0]
check("root skill callerTypes orchestrator×1",
      root_skill["callerTypes"] == {"orchestrator": 1}, root_skill["callerTypes"])
# 零 token F3: bySkill 行无任何 token 字段
check("零 token (行无 input/total)", "input" not in top and "total" not in top, list(top.keys()))
# grandTotal 不含 skill (SubagentCall 的 300 仍是 300)
check("grandTotal total=300 (skill 不入账)", res["grandTotal"]["total"] == 300, res["grandTotal"])

# ===== 组12 · perSession (§9 双数据源: Mode A live 源也吐 perSession, 同形 app.js 契约) =====
from analyze import _per_session_row  # E1: 共享 shaping helper

print("\n[组12] perSession —— live 源按 sessionId 分组吐 perSession 行 (E1, §9 双数据源同形)")
# 合成 2 个 session (不同 sid / projectName), 每个多次 SubagentCall (真实 live record.py 形态).
sess1_calls = [
    subagent("sess-live-1", "general-purpose", "a1", total=28662, inp=430, out=1352, cr=26880,
             dur=98069, tool_use_id="call_x"),
    subagent("sess-live-1", "Explore", "e1", total=1200, inp=800, out=400, cr=0,
             caller_id="a1", caller_type="general-purpose", dur=5000, tool_use_id="call_x2"),
]
sess2_calls = [
    subagent("sess-live-2", "general-purpose", "b1", total=5000, inp=2000, out=1000, cr=2000,
             dur=30000, tool_use_id="call_y"),
    subagent("sess-live-2", "general-purpose", "b2", total=3000, inp=1500, out=500, cr=1000,
             dur=20000, tool_use_id="call_y2"),
    subagent("sess-live-2", "general-purpose", "b3", total=2000, inp=1000, out=500, cr=500,
             dur=10000, tool_use_id="call_y3"),
]
# 改 projectName 以区分两 session (run_jsonl 复用 subagent() 默认 projectName="test", 此处单独改)
for r in sess1_calls:
    r["projectName"] = "proj-alpha"
for r in sess2_calls:
    r["projectName"] = "proj-beta"
res = run_jsonl(sess1_calls + sess2_calls)

ps = res.get("perSession", [])
check("perSession 存在", isinstance(ps, list), type(ps))
check("perSession len==2 (2 session)", len(ps) == 2, len(ps))

EXPECTED_KEYS = {"project", "sid", "generationId", "spawns", "totalTokens", "cacheReadPct",
                 "durationS", "consistent", "modeLabel", "grandTotal", "ctxPeak",
                 "ctxLimitErrors", "rootUsage", "asyncCount", "toolErrorCount"}
check("每行 keys 集合 == app.js 契约 (15 字段, 含 generationId+asyncCount+toolErrorCount; 无漂移)",
      all(set(r.keys()) == EXPECTED_KEYS for r in ps), [sorted(r.keys()) for r in ps])

by_sid = {r["sid"]: r for r in ps}
r1 = by_sid["sess-live-1"]
r2 = by_sid["sess-live-2"]

check("sess1 spawns==2 (两条 SubagentCall)", r1["spawns"] == 2, r1["spawns"])
check("sess2 spawns==3 (三条 SubagentCall)", r2["spawns"] == 3, r2["spawns"])
check("sess1 totalTokens==Σtokens.total (28662+1200=29862)", r1["totalTokens"] == 29862, r1["totalTokens"])
check("sess2 totalTokens==Σtokens.total (5000+3000+2000=10000)", r2["totalTokens"] == 10000, r2["totalTokens"])
# cacheReadPct = round(cacheRead/(cacheRead+input+cacheCreation)*100, 1)  §8.3/红线6 命中率 (output 不进分母, 与 dashboard app.js sessHit 同口径, 2026-06-23 定调; den=0 → 0.0)
#   旧公式 cacheRead/total*100 (output 进分母) 是 F-1 bug, 已废弃.
check("sess1 cacheReadPct==round(26880/(26880+1230+0)*100,1)=95.6 (hit, output 不进分母)",
      r1["cacheReadPct"] == 95.6, r1["cacheReadPct"])
check("sess2 cacheReadPct==round(3500/(3500+4500+0)*100,1)=43.8 (hit)",
      r2["cacheReadPct"] == 43.8, r2["cacheReadPct"])
check("sess1 modeLabel=='A · live'", r1["modeLabel"] == "A · live", r1["modeLabel"])
check("sess2 modeLabel=='A · live'", r2["modeLabel"] == "A · live", r2["modeLabel"])
check("sess1 ctxPeak==0 (live 源无 root context 通道)", r1["ctxPeak"] == 0, r1["ctxPeak"])
check("sess2 ctxPeak==0", r2["ctxPeak"] == 0, r2["ctxPeak"])
check("sess1 project==proj-alpha (取 record projectName)", r1["project"] == "proj-alpha", r1["project"])
check("sess2 project==proj-beta", r2["project"] == "proj-beta", r2["project"])
# durationS = round(ΣdurationMs/1000, 1)
check("sess1 durationS==round(98069+5000)/1000=103.1", r1["durationS"] == 103.1, r1["durationS"])
check("sess2 durationS==round(30000+20000+10000)/1000=60.0", r2["durationS"] == 60.0, r2["durationS"])
check("sess1 consistent==True", r1["consistent"] is True, r1["consistent"])
check("sess2 consistent==True", r2["consistent"] is True, r2["consistent"])
# grandTotal 转发 (四桶)
check("sess1 grandTotal.total==29862", r1["grandTotal"]["total"] == 29862, r1["grandTotal"]["total"])
check("sess2 grandTotal.cacheRead==3500", r2["grandTotal"]["cacheRead"] == 3500, r2["grandTotal"]["cacheRead"])
# 降序按 totalTokens (sess1 29862 > sess2 10000)
check("降序: sess1(29862) 在前", ps[0]["sid"] == "sess-live-1", ps[0]["sid"])
# 原 sessions (裸 sid 列表) 仍向后兼容存在
check("sessions 裸 sid 列表仍存在 (向后兼容)", set(res.get("sessions", [])) == {"sess-live-1", "sess-live-2"},
      res.get("sessions"))

# _per_session_row 纯函数 (防除零: den=cacheRead+input+cacheCreation=0 时 cacheReadPct=0.0)
row = _per_session_row(
    project="px", sid="sx", spawns=5,
    grand_total_dict={"input": 0, "output": 0, "cacheCreation": 0, "cacheRead": 0, "total": 0},
    dur_ms=0, consistent=True, mode_label="A · live", ctx_peak=0,
)
check("helper total=0: 字段集合匹配契约", set(row.keys()) == EXPECTED_KEYS, sorted(row.keys()))
check("helper total=0: cacheReadPct=0.0 防除零", row["cacheReadPct"] == 0.0, row["cacheReadPct"])
check("helper total=0: totalTokens=0", row["totalTokens"] == 0, row["totalTokens"])
check("helper dur_ms=0: durationS=0.0", row["durationS"] == 0.0, row["durationS"])
row2 = _per_session_row(
    project="py", sid="sy", spawns=2,
    grand_total_dict={"input": 100, "output": 50, "cacheCreation": 0, "cacheRead": 950, "total": 1100},
    dur_ms=65500, consistent=False, mode_label="A · live", ctx_peak=42,
)
check("helper 正常: cacheReadPct==round(950/(950+100+0)*100,1)=90.5 (hit)", row2["cacheReadPct"] == 90.5, row2["cacheReadPct"])
check("helper 正常: durationS==round(65500/1000,1)=65.5", row2["durationS"] == 65.5, row2["durationS"])
check("helper 正常: ctx_peak 透传==42", row2["ctxPeak"] == 42, row2["ctxPeak"])
check("helper 正常: consistent 透传==False", row2["consistent"] is False, row2["consistent"])

# ===== 组13 · --watch (C 形态 live-tail, §8.8) 契约 =====
print("\n[组13] --watch —— C 形态 live-tail (§8.8) 契约: --help 含 flag; --watch 不与 --json 冲突")
p = subprocess.run([sys.executable, ANALYZE, "--help"], capture_output=True, text=True)
check("--help exit 0", p.returncode == 0, p.returncode)
check("--help 含 --watch", "--watch" in p.stdout, "absent in --help")
# --watch 与 --json 同给: 两者都 store_true, argparse 不互斥. --watch 会进循环 → 用 timeout 包住:
# 进循环 = 被 timeout 杀 (rc 124); 若 argparse 拒绝组合 = 立即 rc 2 + usage 报错.
wd = tempfile.mkdtemp(prefix="obs-an-watch-")
wfp = os.path.join(wd, "2026-06-16.jsonl")
with open(wfp, "w") as f:
    f.write(json.dumps(subagent("w-sid", "general-purpose", "wa", total=100), ensure_ascii=False) + "\n")
pw = subprocess.run(["timeout", "1.5", sys.executable, ANALYZE, "--watch", "--jsonl", wfp],
                    capture_output=True, text=True)
check("--watch 进循环 (rc==124 timeout 杀, 非 rc==2 argparse 拒绝)",
      pw.returncode == 124, f"rc={pw.returncode} stderr={pw.stderr[:200]}")
check("--watch 循环期无 argparse usage 错", "usage:" not in pw.stderr, pw.stderr[:200])
shutil.rmtree(wd, ignore_errors=True)

# ===== 组14 · Phase 3 load_generations_map (§10.1 lineage 载入, inert-safe 纯函数) =====
print("\n[组14] load_generations_map — 缺文件/坏行/external 形/last-writer-wins 容错")
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
import analyze as _az   # 直接 import 测纯函数 (subprocess 跑 CLI 见组1-13); analyze import 已含 try/except 守卫
_gd = tempfile.mkdtemp(prefix="obs-gen-")
# 14a 缺文件 → ({}, [])
m, raw = _az.load_generations_map(log_base=_gd)
check("缺文件 → 空 map + 空 raw", m == {} and raw == [], (m, raw))
# 14b well-formed → 正确映射 (两 sid 同 gid = 缝合前提)
_gp = os.path.join(_gd, "generations.jsonl")
with open(_gp, "w") as f:
    f.write(json.dumps({"recordType": "GenerationLineage", "sessionId": "s1", "generationId": "g1",
                        "carrierSource": "env", "source": "startup", "writer": "plugin-hook"}) + "\n")
    f.write(json.dumps({"recordType": "GenerationLineage", "sessionId": "s2", "generationId": "g1",
                        "carrierSource": "env", "source": "resume", "writer": "plugin-hook"}) + "\n")
m, raw = _az.load_generations_map(log_base=_gd)
check("well-formed: 2 sid → 同 g1", m == {"s1": "g1", "s2": "g1"}, m)
check("well-formed: raw 2 行", len(raw) == 2, len(raw))
# 14c 坏行混好行 → 跳坏不抛
with open(_gp, "w") as f:
    f.write("not json\n")                                          # 非法 JSON → 跳
    f.write(json.dumps({"sessionId": "s3", "generationId": "g3"}) + "\n")   # 好行
    f.write(json.dumps({"generationId": "gX"}) + "\n")             # 无 sessionId → 跳
    f.write(json.dumps({"sessionId": "s4", "generationId": "g4", "extra": "ignored"}) + "\n")  # 好行, 未知字段忽略
m, raw = _az.load_generations_map(log_base=_gd)
check("坏行跳过不抛: 只 s3/s4 进 map", m == {"s3": "g3", "s4": "g4"}, m)
check("坏行跳过: raw 只含 2 好行 dict", len(raw) == 2, len(raw))
# 14d external 形 (ts/prevSessionId/writer=external) 容错 — reader 不要求 plugin-hook
with open(_gp, "w") as f:
    f.write(json.dumps({"generationId": "gB", "sessionId": "sB", "writer": "external",
                        "ts": "2026-06-22T10:00:00Z", "prevSessionId": "sA"}) + "\n")
m, raw = _az.load_generations_map(log_base=_gd)
check("external 形 (ts/prevSessionId/writer=external) 容错", m == {"sB": "gB"}, m)
# 14e last-writer-wins: 同 sid 两行, 后盖前 (external 应盖 plugin-hook)
with open(_gp, "w") as f:
    f.write(json.dumps({"sessionId": "sX", "generationId": "g-old", "writer": "plugin-hook"}) + "\n")
    f.write(json.dumps({"sessionId": "sX", "generationId": "g-new", "writer": "external"}) + "\n")
m, _r = _az.load_generations_map(log_base=_gd)
check("last-writer-wins: 同 sid 后写盖前", m.get("sX") == "g-new", m)
shutil.rmtree(_gd, ignore_errors=True)

# ===== 组15 · Phase 3 _apply_generation_map (post-ingest 缝合, 就地改 records) =====
print("\n[组15] _apply_generation_map — Mode B 查 map 恢复; 空 map no-op; 不覆盖已有 carrierSource")
# 15a 空 map → 不动 (今天行为, inert-safe)
recs = [{"sessionId": "s1", "generationId": "s1", "carrierSource": None},
        {"sessionId": "s2", "generationId": "s2", "carrierSource": None}]
_az._apply_generation_map(recs, {})
check("空 map: generationId 仍=sid", all(r["generationId"] == r["sessionId"] for r in recs), recs)
check("空 map: carrierSource 仍 None", all(r["carrierSource"] is None for r in recs), recs)
# 15b 命中 map → 覆盖 generationId + carrierSource=lineage-map (仅命中的); 未命中不动
recs = [{"sessionId": "s1", "generationId": "s1", "carrierSource": None},
        {"sessionId": "s2", "generationId": "s2", "carrierSource": None}]
_az._apply_generation_map(recs, {"s1": "g-shared"})
check("命中 s1: generationId=g-shared", recs[0]["generationId"] == "g-shared", recs[0])
check("命中 s1: carrierSource=lineage-map", recs[0]["carrierSource"] == "lineage-map", recs[0])
check("未命中 s2: generationId 仍=s2", recs[1]["generationId"] == "s2", recs[1])
check("未命中 s2: carrierSource 仍 None", recs[1]["carrierSource"] is None, recs[1])
# 15c 已有 carrierSource 不被覆盖 (只补空 carrierSource)
recs = [{"sessionId": "s1", "generationId": "s1", "carrierSource": "env"}]
_az._apply_generation_map(recs, {"s1": "g-x"})
check("命中但原 carrierSource=env: 保 env 不覆盖", recs[0]["carrierSource"] == "env", recs[0])
check("命中但原 carrierSource=env: generationId 仍覆盖", recs[0]["generationId"] == "g-x", recs[0])
# 15d Mode A live 形 (generationId 已=carrier 值, 与 map 同) → no-op 确认 (不重置 carrierSource)
recs = [{"sessionId": "s1", "generationId": "g-live", "carrierSource": "env"}]
_az._apply_generation_map(recs, {"s1": "g-live"})
check("Mode A live 值==map 值: generationId no-op", recs[0]["generationId"] == "g-live", recs[0])
check("Mode A live 值==map 值: carrierSource 保 env", recs[0]["carrierSource"] == "env", recs[0])

# ===== 组16 · Phase 3 aggregate_generations (跨 session 卷起 + singleton 回退) =====
print("\n[组16] aggregate_generations — 同 generationId 卷起; 全 singleton=今天行为")
def _psrow(sid, gid, spawns, total, dur_s):
    """合成 per_session_row (只填 aggregate_generations 用到的字段)."""
    return {"sid": sid, "generationId": gid, "spawns": spawns,
            "grandTotal": {"input": 0, "output": 0, "cacheCreation": 0, "cacheRead": 0, "total": total},
            "durationS": dur_s}
# 16a 两 session 共享 gid + 一个 singleton → 2 generation
rows = [_psrow("s1", "g-shared", 3, 1000, 10.0),
        _psrow("s2", "g-shared", 5, 2000, 20.0),
        _psrow("s-solo", "s-solo", 2, 500, 5.0)]   # gid==sid → singleton
gens = _az.aggregate_generations(rows)
check("2 generation (一 shared 一 singleton)", len(gens) == 2, len(gens))
by_gid = {g["generationId"]: g for g in gens}
shared = by_gid["g-shared"]
check("shared multiSession=True", shared["multiSession"] is True, shared)
check("shared sessionsN=2", shared["sessionsN"] == 2, shared)
check("shared sessionIds=[s1,s2]", shared["sessionIds"] == ["s1", "s2"], shared)
check("shared spawnsTotal=8 (3+5)", shared["spawnsTotal"] == 8, shared)
check("shared grandTotal.total=3000 (1000+2000)", shared["grandTotal"]["total"] == 3000, shared["grandTotal"])
check("shared durationS=30.0 (10+20)", shared["durationS"] == 30.0, shared["durationS"])
solo = by_gid["s-solo"]
check("singleton multiSession=False", solo["multiSession"] is False, solo)
check("singleton sessionsN=1", solo["sessionsN"] == 1, solo)
# 16b 按 grandTotal.total 降序 (shared 3000 > solo 500)
check("降序: shared(3000) 在前", gens[0]["generationId"] == "g-shared", [g["generationId"] for g in gens])
# 16c inert (全 gid==sid) → 全 singleton, 今天行为
gens_inert = _az.aggregate_generations([_psrow("a", "a", 1, 100, 1.0), _psrow("b", "b", 1, 200, 2.0)])
check("inert (全 gid==sid): 全 singleton", all(not g["multiSession"] for g in gens_inert),
      [g["multiSession"] for g in gens_inert])
check("inert: 2 generation", len(gens_inert) == 2, len(gens_inert))
# 16d 空 per_session_rows → []
check("空 per_session_rows → []", _az.aggregate_generations([]) == [], "non-empty")

# ===== 组17 · 缺口1 budgetState (reader-computes, threshold env opt-in; inert 不加 key) =====
print("\n[组17] budgetState — reader-computes 预算; threshold env opt-in (未配 → 无 key, 逐字今天行为)")
# 17a _budget_threshold: env 解析 (空/非数字/0 → None)
_env_save = os.environ.pop("AGENTINSIGHT_BUDGET_THRESHOLD", None)
try:
    os.environ["AGENTINSIGHT_BUDGET_THRESHOLD"] = "60000"
    check("_budget_threshold 有效 int=60000", _az._budget_threshold() == 60000, _az._budget_threshold())
    os.environ["AGENTINSIGHT_BUDGET_THRESHOLD"] = "  12345  "
    check("_budget_threshold strip 空白=12345", _az._budget_threshold() == 12345, _az._budget_threshold())
    os.environ["AGENTINSIGHT_BUDGET_THRESHOLD"] = "not-a-number"
    check("_budget_threshold 非数字 → None", _az._budget_threshold() is None, _az._budget_threshold())
    os.environ["AGENTINSIGHT_BUDGET_THRESHOLD"] = "0"
    check("_budget_threshold '0' → None (零阈值无意义)", _az._budget_threshold() is None, _az._budget_threshold())
    os.environ["AGENTINSIGHT_BUDGET_THRESHOLD"] = ""
    check("_budget_threshold 空串 → None", _az._budget_threshold() is None, _az._budget_threshold())
    del os.environ["AGENTINSIGHT_BUDGET_THRESHOLD"]
    check("_budget_threshold 缺失 → None", _az._budget_threshold() is None, _az._budget_threshold())
finally:
    if _env_save is None:
        os.environ.pop("AGENTINSIGHT_BUDGET_THRESHOLD", None)
    else:
        os.environ["AGENTINSIGHT_BUDGET_THRESHOLD"] = _env_save
check("_budget_threshold 未污染 env (已还原)", "AGENTINSIGHT_BUDGET_THRESHOLD" not in os.environ, os.environ.get("AGENTINSIGHT_BUDGET_THRESHOLD"))

# 17b _budget_state: 纯函数数学 (pct 取整 / exceeded 到阈即超 / None inert)
check("_budget_state threshold=None → None", _az._budget_state(500, None) is None, "non-None")
check("_budget_state threshold=0 → None (inert)", _az._budget_state(500, 0) is None, "non-None")
bs = _az._budget_state(400, 500)
check("_budget_state 400/500 exceeded=False", bs["exceeded"] is False, bs)
check("_budget_state pct 取整 80.0", bs["pctOfThreshold"] == 80.0, bs)
check("_budget_state cumulativeTotal=400", bs["cumulativeTotal"] == 400, bs)
check("_budget_state threshold=500", bs["threshold"] == 500, bs)
bs_edge = _az._budget_state(500, 500)
check("_budget_state 到阈即超 (cumulative==threshold → exceeded True)", bs_edge["exceeded"] is True, bs_edge)
check("_budget_state pct 100.0", bs_edge["pctOfThreshold"] == 100.0, bs_edge)
bs_over = _az._budget_state(750, 500)
check("_budget_state 超阈 750/500 exceeded=True", bs_over["exceeded"] is True, bs_over)
check("_budget_state pct 150.0", bs_over["pctOfThreshold"] == 150.0, bs_over)
bs_odd = _az._budget_state(1, 3)
check("_budget_state pct 非整除 1/3=33.3 (round 1 位)", bs_odd["pctOfThreshold"] == 33.3, bs_odd)

# 17c aggregate_generations(threshold=T): 跨 session 卷起 cumulativeTotal = 两 session total 之和
rows_b = [_psrow("s1", "g-shared", 3, 150, 10.0), _psrow("s2", "g-shared", 5, 250, 20.0),
          _psrow("s-solo", "s-solo", 2, 100, 5.0)]
gens_b = _az.aggregate_generations(rows_b, threshold=500)
by_gid_b = {g["generationId"]: g for g in gens_b}
shared_b = by_gid_b["g-shared"]
check("multiSession budgetState key 存在", "budgetState" in shared_b, list(shared_b.keys()))
check("multiSession cumulativeTotal=400 (150+250 跨 session 之和)",
      shared_b["budgetState"]["cumulativeTotal"] == 400, shared_b["budgetState"])
check("multiSession pct=80.0", shared_b["budgetState"]["pctOfThreshold"] == 80.0, shared_b["budgetState"])
check("multiSession exceeded=False (400<500)", shared_b["budgetState"]["exceeded"] is False, shared_b["budgetState"])
check("multiSession threshold=500", shared_b["budgetState"]["threshold"] == 500, shared_b["budgetState"])
solo_b = by_gid_b["s-solo"]
check("singleton 也有 budgetState key", "budgetState" in solo_b, list(solo_b.keys()))
check("singleton cumulativeTotal=100", solo_b["budgetState"]["cumulativeTotal"] == 100, solo_b["budgetState"])
check("singleton pct=20.0", solo_b["budgetState"]["pctOfThreshold"] == 20.0, solo_b["budgetState"])

# 17d inert: threshold=None (显式 + 默认参) → 无 budgetState key, result 逐字今天
check("inert threshold=None: 无 budgetState key",
      all("budgetState" not in g for g in _az.aggregate_generations(rows_b, threshold=None)),
      [list(g.keys()) for g in _az.aggregate_generations(rows_b, threshold=None)])
check("inert 默认参 (不传 threshold): 无 budgetState key",
      all("budgetState" not in g for g in _az.aggregate_generations(rows_b)),
      [list(g.keys()) for g in _az.aggregate_generations(rows_b)])
check("inert: 其余字段与 threshold=None 逐字相同 (budget 是加性, 不改既有)",
      _az.aggregate_generations(rows_b, threshold=500) and True  # 上方已校验既有字段组16 覆盖
      and [{k: v for k, v in g.items() if k != "budgetState"} for g in _az.aggregate_generations(rows_b, threshold=500)]
      == _az.aggregate_generations(rows_b, threshold=None), "diff")

# 17e tier 数学边界: over (>=) / 单 session 超阈
gens_over = _az.aggregate_generations([_psrow("s1", "g1", 1, 600, 1.0)], threshold=500)
check("tier over (600>=500) exceeded=True", gens_over[0]["budgetState"]["exceeded"] is True, gens_over[0]["budgetState"])
check("tier over pct=120.0", gens_over[0]["budgetState"]["pctOfThreshold"] == 120.0, gens_over[0]["budgetState"])
gens_eq = _az.aggregate_generations([_psrow("s1", "g1", 1, 500, 1.0)], threshold=500)
check("tier 到阈 (500==500) exceeded=True (>=)", gens_eq[0]["budgetState"]["exceeded"] is True, gens_eq[0]["budgetState"])

# ===== 收尾 =====
print("\n" + "=" * 70)
print(f"结果: {passed} PASS / {failed} FAIL")
print("验证范围: reader IR 重建 + §7 路径A 拓扑 + 自洽诊断 (Mode A · own-JSONL).")
print("Mode B (喂 CC transcript, §9.2·B) 已交付 — 见 tests/test_transcript_adapter.py (45/45).")
print("=" * 70)
sys.exit(1 if failed else 0)
