#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent-insight Mode B · scan-projects (analyze.py --scan-projects) 单测.

【隔离保证】(同 test_analyze.py / test_transcript_adapter.py 打法)
  - 合成 <tmp>/<proj>/<sid>.jsonl [+ <tmp>/<proj>/<sid>/subagents/agent-*.jsonl] 写进 tempfile,
    子进程 `analyze.py --scan-projects <tmp> --json` 跑;
  - 不碰真 ~/.claude / 真 transcript / 真 session / settings.json / marketplace.json.
  => 对当前 session 零影响.

测的是 Mode B fleet 扫描: discover_root_transcripts → 逐 session load_transcript → 跨 session 聚合
(run_scan / aggregate_scan / _merge_*) → render_scan / --json.
  - sessionsScanned / spawnsTotal / grandTotal 求和;
  - by_subagent_type 跨 session 合并正确性 (率用原始累加器重算, 不能平均平均 — S2 头号用例);
  - topSessions 排序 / per-session 汇总 / scanConsistency 聚合;
  - --project 过滤 / 互斥 / --json 字段齐全 / 人类表格 / 空目录 / 坏行容错.

sid 须 UUID 形 (discover_root_transcripts 的过滤规则); 故用合法 UUID.

【两处诚实 reframe】(adapter 的防御性设计所致, 非 bug):
  - S6: 坏 JSONL 行在 adapter 层被 per-line try/except 计成 skippedBadLines, **不抛异常** →
    不进 errors[]。errors[] 是更深层故障 (OS 错 / 未来 bug) 的 backstop, 经 subprocess 难以
    注入; 故 S6 改测"坏行 graceful 不崩 + 同 session 好行仍解析 + 其他 session 照扫"。
  - S11: Mode B **一致 by construction** (root 文件 → caller=null/isRoot=true; agent 文件 →
    caller=aid/isRoot=false), isRoot 不变量恒 hold → 经 transcript 合成**无法**造出违例。
    scanConsistency 的违例聚合逻辑改由 unit 级直调 aggregate_scan 验 (S11)。
"""
import json
import os
import sys
import shutil
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(HERE, "..", "tools")
ANALYZE = os.path.join(TOOLS, "analyze.py")

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}   {detail}")


# ---------- 合成 transcript 行构造器 (简化自 test_transcript_adapter) ----------
def agent_line(agent_id, agent_type="Explore", status="completed",
               total_tokens=10000, inp=1000, out=500, ccr=0, cr=8500,
               dur_ms=5000, resolved_model=None, ts="2026-06-16T13:00:00+08:00"):
    """一条带 Agent toolUseResult (dict + agentId) 的 transcript 行 (root-direct spawn)."""
    tur = {"status": status, "agentId": agent_id, "agentType": agent_type,
           "totalDurationMs": dur_ms, "usage": {
               "input_tokens": inp, "output_tokens": out,
               "cache_creation_input_tokens": ccr, "cache_read_input_tokens": cr}}
    if total_tokens is not None:
        tur["totalTokens"] = total_tokens
    if resolved_model is not None:
        tur["resolvedModel"] = resolved_model
    return json.dumps({"timestamp": ts, "sessionId": "s-x", "isSidechain": False,
                       "type": "assistant", "uuid": "u-" + agent_id,
                       "message": {"role": "assistant"}, "toolUseResult": tur}, ensure_ascii=False)


_SKILL_SEQ = [0]   # 跨调用唯一 tool_use_id (多次同名 skill 不覆写 pending)


def skill_line(command_name, success=True, ts="2026-06-16T13:05:00+08:00"):
    """一条 ROOT Skill 调用 = assistant tool_use(Skill) + user tool_result(带顶层 tur) 两行
    (镜像 real CC; D4 单源 tool_use 采集 + success 反查). 返回 newline-joined 2 行 jsonl.
    镜像 test_transcript_adapter.skill_result_line."""
    _SKILL_SEQ[0] += 1
    n = _SKILL_SEQ[0]
    tuid = "tu-skill-%d" % n
    tur = {"success": success, "commandName": command_name, "allowedTools": []}
    assistant_line = json.dumps({"timestamp": ts, "sessionId": "s-x", "isSidechain": False,
                                 "type": "assistant", "uuid": "u-skill-%d" % n,
                                 "message": {"role": "assistant", "id": "msg-skill-%d" % n, "content": [
                                     {"type": "tool_use", "id": tuid, "name": "Skill",
                                      "input": {"skill": command_name}}]}}, ensure_ascii=False)
    user_line = json.dumps({"timestamp": ts, "sessionId": "s-x", "isSidechain": False,
                            "type": "user", "uuid": "u-skillres-%d" % n,
                            "message": {"role": "user", "content": [
                                {"type": "tool_result", "tool_use_id": tuid}]},
                            "toolUseResult": tur}, ensure_ascii=False)
    return assistant_line + "\n" + user_line


# 合法 UUID 形 sid (discover_root_transcripts 的过滤规则)
SID_A = "00000000-0000-4000-8000-00000000000a"
SID_B = "00000000-0000-4000-8000-00000000000b"
SID_C = "00000000-0000-4000-8000-00000000000c"


def build_session(tmp, proj, sid, root_lines, subagents=None):
    """合成 <tmp>/<proj>/<sid>.jsonl [+ <tmp>/<proj>/<sid>/subagents/agent-*.jsonl]."""
    pdir = os.path.join(tmp, proj)
    os.makedirs(pdir, exist_ok=True)
    with open(os.path.join(pdir, sid + ".jsonl"), "w") as f:
        for ln in root_lines:
            f.write(ln + "\n")
    if subagents:
        sub_dir = os.path.join(pdir, sid, "subagents")
        os.makedirs(sub_dir, exist_ok=True)
        for aid, lines in subagents.items():
            with open(os.path.join(sub_dir, "agent-%s.jsonl" % aid), "w") as f:
                for ln in lines:
                    f.write(ln + "\n")


def run_scan(tmp, project=None, json_mode=True, extra=None, env=None):
    cmd = [sys.executable, ANALYZE, "--scan-projects", tmp]
    if project:
        cmd += ["--project", project]
    if json_mode:
        cmd += ["--json"]
    if extra:
        cmd += extra
    p = subprocess.run(cmd, capture_output=True, text=True, env=env)   # env=None → 继承父 (S1-S11 不变); S12+ 显式 AGENTINSIGHT_LOG_DIR 隔离测 lineage map
    res = {"_rc": p.returncode, "_stderr": p.stderr, "_stdout": p.stdout}
    if json_mode and p.returncode == 0:
        try:
            res.update(json.loads(p.stdout))
        except Exception:
            pass
    return res


print("=" * 70)
print("agent-insight Mode B · scan-projects 单测 (隔离)")
print("=" * 70)

# ===== S1 两 session 各 root+spawn → sessionsScanned=2 / grandTotal 求和 =====
print("\n[S1] 两 session — sessionsScanned=2, perSession=2, grandTotal=两 session 求和")
d = tempfile.mkdtemp(prefix="obs-scan-s1-")
build_session(d, "fleet", SID_A, [agent_line("a1", total_tokens=1000)])
build_session(d, "fleet", SID_B, [agent_line("a2", total_tokens=2000)])
res = run_scan(d)
check("exit 0", res.get("_rc") == 0, res.get("_stderr"))
check("mode=B · scan-projects", res.get("mode") == "B · scan-projects", res.get("mode"))
check("sessionsScanned=2", res.get("sessionsScanned") == 2, res.get("sessionsScanned"))
check("spawnsTotal=2", res.get("spawnsTotal") == 2, res.get("spawnsTotal"))
check("grandTotal.total=3000 (1000+2000)", res.get("grandTotal", {}).get("total") == 3000, res.get("grandTotal"))
check("perSession 长度=2", len(res.get("perSession", [])) == 2, res.get("perSession"))
check("errors=0", len(res.get("errors", [])) == 0, res.get("errors"))
shutil.rmtree(d, ignore_errors=True)

# ===== S2 by_subagent_type 跨 session 合并正确性 (头号用例: 率不能平均平均) =====
print("\n[S2] 跨 session 合并 — dur 1000+3000+5000 / 2 成功 3 calls → avg=3000 / rate=0.667")
d = tempfile.mkdtemp(prefix="obs-scan-s2-")
# session A: 2×Explore (dur 1000 completed + dur 3000 error) → avg 2000 / rate 0.5
build_session(d, "fleet", SID_A, [
    agent_line("a1", "Explore", status="completed", dur_ms=1000, total_tokens=100, ts="2026-06-16T13:00:00+08:00"),
    agent_line("a2", "Explore", status="error", dur_ms=3000, total_tokens=100, ts="2026-06-16T13:00:01+08:00"),
])
# session B: 1×Explore (dur 5000 completed) → avg 5000 / rate 1.0
build_session(d, "fleet", SID_B, [
    agent_line("b1", "Explore", status="completed", dur_ms=5000, total_tokens=100, ts="2026-06-16T13:00:02+08:00"),
])
res = run_scan(d)
bt = res.get("bySubagentType", [])
exp = next((r for r in bt if r["subagentType"] == "Explore"), {})
check("Explore calls=3 (2+1)", exp.get("calls") == 3, exp)
check("avgDurationMs=3000 (durSum 9000/durN 3, 非 3500)", exp.get("avgDurationMs") == 3000.0, exp)
check("successRate=0.667 (successCount 2/calls 3, 非 0.75)", exp.get("successRate") == 0.667, exp)
# 反例锁定: 朴素平均会给 avg 3500 / rate 0.75
check("反例: avg != 3500 (朴素平均错)", exp.get("avgDurationMs") != 3500.0, exp)
check("反例: rate != 0.75 (朴素平均错)", exp.get("successRate") != 0.75, exp)
shutil.rmtree(d, ignore_errors=True)

# ===== S3 topSessions 按 totalTokens 降序, ≤10 行 =====
print("\n[S3] topSessions — 按 totalTokens 降序, ≤10")
d = tempfile.mkdtemp(prefix="obs-scan-s3-")
build_session(d, "fleet", SID_A, [agent_line("a1", total_tokens=3000)])
build_session(d, "fleet", SID_B, [agent_line("a2", total_tokens=1000)])
build_session(d, "fleet", SID_C, [agent_line("a3", total_tokens=2000)])
res = run_scan(d)
top = res.get("topSessions", [])
totals = [r["totalTokens"] for r in top]
check("topSessions ≤10", len(top) <= 10, len(top))
check("topSessions 降序", totals == sorted(totals, reverse=True), totals)
check("topSessions 首位=最高 (3000)", top and top[0]["totalTokens"] == 3000, totals)
shutil.rmtree(d, ignore_errors=True)

# ===== S4 单 session 无 <sid>/subagents/ (只 root) → 仍 scanned, 不崩 =====
print("\n[S4] 单 session 无 subagents/ (只 root) — 仍 scanned, spawns from root, exit 0")
d = tempfile.mkdtemp(prefix="obs-scan-s4-")
build_session(d, "fleet", SID_A, [agent_line("a1", total_tokens=500)])   # 无 subagents=
res = run_scan(d)
check("exit 0 (无 subagents/ 不崩)", res.get("_rc") == 0, res.get("_stderr"))
check("sessionsScanned=1", res.get("sessionsScanned") == 1, res.get("sessionsScanned"))
check("spawnsTotal=1 (root-direct)", res.get("spawnsTotal") == 1, res.get("spawnsTotal"))
check("grandTotal.total=500", res.get("grandTotal", {}).get("total") == 500, res.get("grandTotal"))
shutil.rmtree(d, ignore_errors=True)

# ===== S5 scan 空目录 → sessionsScanned=0, grandTotal 全 0, exit 0 =====
print("\n[S5] 空目录 — sessionsScanned=0, grandTotal 全 0, exit 0")
d = tempfile.mkdtemp(prefix="obs-scan-s5-")
res = run_scan(d)
check("exit 0 (空目录不崩)", res.get("_rc") == 0, res.get("_stderr"))
check("sessionsScanned=0", res.get("sessionsScanned") == 0, res.get("sessionsScanned"))
check("grandTotal.total=0", res.get("grandTotal", {}).get("total") == 0, res.get("grandTotal"))
check("spawnsTotal=0", res.get("spawnsTotal") == 0, res.get("spawnsTotal"))
check("scanConsistency.allConsistent=True (无数据)", res.get("scanConsistency", {}).get("allConsistent") is True, res.get("scanConsistency"))
shutil.rmtree(d, ignore_errors=True)

# ===== S6 坏 JSONL 行容错 (per-session 隔离, graceful 非 error) =====
print("\n[S6] 坏 JSONL 行 — graceful skipped, 非 error, 其他 session 照扫, exit 0")
d = tempfile.mkdtemp(prefix="obs-scan-s6-")
# session A: 仅垃圾行 (非 JSON) → 0 valid spawn → skipped
build_session(d, "fleet", SID_A, ["this is not json {{{", "{ also broken"])
# session B: 1 干净 spawn
build_session(d, "fleet", SID_B, [agent_line("b1", total_tokens=777)])
res = run_scan(d)
check("exit 0 (坏行不崩)", res.get("_rc") == 0, res.get("_stderr"))
check("sessionsScanned=2 (坏 session 仍被处理)", res.get("sessionsScanned") == 2, res.get("sessionsScanned"))
check("sessionsSkipped=1 (坏 session 0 spawn)", res.get("sessionsSkipped") == 1, res.get("sessionsSkipped"))
check("spawnsTotal=1 (好 session 的 spawn 仍在)", res.get("spawnsTotal") == 1, res.get("spawnsTotal"))
check("grandTotal.total=777 (好 session)", res.get("grandTotal", {}).get("total") == 777, res.get("grandTotal"))
check("errors=0 (坏行计 skipped 非 error)", len(res.get("errors", [])) == 0, res.get("errors"))
shutil.rmtree(d, ignore_errors=True)

# ===== S7 --project 过滤 → 只扫指定 project 子目录 =====
print("\n[S7] --project 过滤 — 只扫指定 project")
d = tempfile.mkdtemp(prefix="obs-scan-s7-")
build_session(d, "p1", SID_A, [agent_line("a1", total_tokens=111)])
build_session(d, "p2", SID_B, [agent_line("a2", total_tokens=222)])
res = run_scan(d, project="p1")
check("exit 0", res.get("_rc") == 0, res.get("_stderr"))
check("sessionsScanned=1 (只 p1)", res.get("sessionsScanned") == 1, res.get("sessionsScanned"))
check("perSession project=p1", res.get("perSession") and res["perSession"][0]["project"] == "p1", res.get("perSession"))
check("grandTotal.total=111 (非 333, p2 被滤掉)", res.get("grandTotal", {}).get("total") == 111, res.get("grandTotal"))
shutil.rmtree(d, ignore_errors=True)

# ===== S8 --scan-projects 与 --transcript 互斥 → exit 1 + stderr =====
print("\n[S8] 互斥 — --scan-projects + --transcript → exit 1 + stderr 提示")
d = tempfile.mkdtemp(prefix="obs-scan-s8-")
p = subprocess.run([sys.executable, ANALYZE, "--scan-projects", d, "--transcript", os.path.join(d, "x.jsonl")],
                   capture_output=True, text=True)
check("exit 1 (互斥)", p.returncode == 1, p.returncode)
check("stderr 含 '互斥'", "互斥" in p.stderr, p.stderr)
shutil.rmtree(d, ignore_errors=True)

# ===== S9 --json 字段齐全 =====
print("\n[S9] --json 字段齐全 — perSession/topSessions/scanConsistency/depth2Note/bySubagentType")
d = tempfile.mkdtemp(prefix="obs-scan-s9-")
build_session(d, "fleet", SID_A, [agent_line("a1", total_tokens=1000)])
res = run_scan(d)
for k in ["mode", "scanDir", "project", "sessionsScanned", "sessionsSkipped", "errors",
          "grandTotal", "bySubagentType", "callGraph", "spawnsTotal",
          "perSession", "topSessions", "scanConsistency", "depth2Note"]:
    check(f"--json 含 {k}", k in res, list(res.keys()))
ps = (res.get("perSession") or [{}])[0]
for k in ["project", "sid", "spawns", "totalTokens", "cacheReadPct", "durationS", "consistent", "modeLabel"]:
    check(f"perSession 行含 {k}", k in ps, list(ps.keys()))
# Task 1: perSession 行带四桶 grandTotal (转发 run_scan 已算的 m["grandTotal"], 零新聚合)
gt = ps.get("grandTotal")
check("perSession[].grandTotal 四桶齐全 (Task 1 转发)",
      gt is not None and set(gt.keys()) == {"input", "output", "cacheCreation", "cacheRead", "total"},
      list(gt.keys()) if gt else None)
check("perSession[].grandTotal.total == totalTokens (一致性)",
      gt is not None and gt.get("total") == ps.get("totalTokens"),
      {"grandTotal.total": gt.get("total") if gt else None, "totalTokens": ps.get("totalTokens")})
check("depth2Note 标 depth-2 (§9.3#1)", "depth-2" in res.get("depth2Note", ""), res.get("depth2Note"))
shutil.rmtree(d, ignore_errors=True)

# ===== S10 人类表格 (非 --json) → exit 0, 含 per-session 行 + depth-2 banner =====
print("\n[S10] 人类表格 — exit 0, 含 per-session 行 + depth-2 banner")
d = tempfile.mkdtemp(prefix="obs-scan-s10-")
build_session(d, "fleet", SID_A, [agent_line("a1", total_tokens=1000)])
p = subprocess.run([sys.executable, ANALYZE, "--scan-projects", d], capture_output=True, text=True)
check("exit 0", p.returncode == 0, p.stderr)
out = p.stdout
check("stdout 含 'scan-projects'", "scan-projects" in out, out[:200])
check("stdout 含 'per-session'", "per-session" in out, out[:400])
check("stdout 含 'depth-2' banner", "depth-2" in out, out[:400])
check("stdout 含 'cross-session totals'", "cross-session totals" in out, out)
check("stdout 含 'scan self-consistency'", "scan self-consistency" in out, out)
shutil.rmtree(d, ignore_errors=True)

# ===== S11 scanConsistency 违例聚合 (unit 级直调 aggregate_scan; Mode B 一致 by construction) =====
print("\n[S11] scanConsistency 违例聚合 — unit 级直调 aggregate_scan (Mode B 无法造违例)")
sys.path.insert(0, TOOLS)
import analyze  # noqa: E402
fake = [
    {"project": "p", "sid": SID_A, "spawns": 1, "files": 1, "skippedBadLines": 0,
     "grandTotal": {"input": 0, "output": 0, "cacheCreation": 0, "cacheRead": 0, "total": 100},
     "bySubagentTypeRaw": [{"subagentType": "Explore", "calls": 1, "input": 0, "output": 0,
                            "cacheCreation": 0, "cacheRead": 0, "total": 100, "avgDurationMs": 1000.0,
                            "successRate": 1.0, "durSum": 1000, "durN": 1, "successCount": 1}],
     "bySkillRaw": [],
     "callGraph": [{"parentType": "orchestrator", "childType": "Explore", "count": 1}],
     "consistency": {"consistent": True}, "modeLabel": "B · transcript"},
    {"project": "p", "sid": SID_B, "spawns": 1, "files": 1, "skippedBadLines": 0,
     "grandTotal": {"input": 0, "output": 0, "cacheCreation": 0, "cacheRead": 0, "total": 200},
     "bySubagentTypeRaw": [{"subagentType": "Plan", "calls": 1, "input": 0, "output": 0,
                            "cacheCreation": 0, "cacheRead": 0, "total": 200, "avgDurationMs": 2000.0,
                            "successRate": 1.0, "durSum": 2000, "durN": 1, "successCount": 1}],
     "bySkillRaw": [],
     "callGraph": [{"parentType": "orchestrator", "childType": "Plan", "count": 1}],
     "consistency": {"consistent": False}, "modeLabel": "B · transcript"},
]
ares = analyze.aggregate_scan(fake, [], "<dir>", None)
check("allConsistent=False (SID_B 违例)", ares["scanConsistency"]["allConsistent"] is False, ares["scanConsistency"])
check("violatingSessions 含 SID_B", SID_B in ares["scanConsistency"]["violatingSessions"], ares["scanConsistency"])
check("grandTotal.total=300 (100+200 合并)", ares["grandTotal"]["total"] == 300, ares["grandTotal"])
check("bySubagentType 合并 2 type (Explore+Plan)", len(ares["bySubagentType"]) == 2, ares["bySubagentType"])
# 全 consistent 时 allConsistent=True (对照组)
ares2 = analyze.aggregate_scan([dict(fake[0]), dict(fake[1])], [], "<dir>", None)
ares2["perSession"]  # noop
check("全 consistent → allConsistent=True (对照)",
      analyze.aggregate_scan([{**fake[0], "consistency": {"consistent": True}},
                              {**fake[1], "consistency": {"consistent": True}}], [], "<dir>", None)
      ["scanConsistency"]["allConsistent"] is True, "control")

# ===== S12 · fleet 跨 session bySkill 合并 (§8.11) =====
print("\n[S12] 两 session 各调 skill → fleet bySkill 合并 calls/distinct sessions/callerTypes")
d = tempfile.mkdtemp(prefix="obs-scan-s12-")
# session A: root 直发 deep-research 2 次 + brainstorming 1 次; 还 spawn a1(Explore)
build_session(d, "fleet", SID_A, [
    agent_line("a1", "Explore", total_tokens=1000),
    skill_line("deep-research"), skill_line("deep-research"),
    skill_line("superpowers:brainstorming"),
])
# session B: root 直发 deep-research 1 次 (跨 session distinct sessions=2, calls=3)
build_session(d, "fleet", SID_B, [skill_line("deep-research")])
res = run_scan(d)
check("exit 0", res.get("_rc") == 0, res.get("_stderr"))
bs = res.get("bySkill", [])
check("bySkill 2 行 (deep-research + brainstorming)", len(bs) == 2, bs)
dr = [r for r in bs if r["skillName"] == "deep-research"][0]
check("deep-research calls=3 (2+1 跨 session 求和)", dr["calls"] == 3, dr)
check("deep-research sessions=2 (两 session 都调过)", dr["sessions"] == 2, dr)
check("deep-research callerTypes orchestrator×3 (都 root 直发)",
      dr["callerTypes"] == {"orchestrator": 3}, dr["callerTypes"])
br = [r for r in bs if r["skillName"] == "superpowers:brainstorming"][0]
check("brainstorming sessions=1 (只 session A)", br["sessions"] == 1, br)

# ===== S13 · perSession ctxPeak == root 主线 context 峰值 (Plan 3a 数据层接入 scan) =====
print("\n[S13] root assistant usage turn (ctx=9000) → perSession.ctxPeak; 无 root usage session ctxPeak==0 但字段在")
d = tempfile.mkdtemp(prefix="obs-scan-s13-")
# root 主线 assistant turn 自带 message.usage (input 3000 + cacheRead 6000 → ctx 9000).
# (helpers 不产 root message.usage 行 —— agent_line 的 usage 在 toolUseResult, 非 root 主线 turn; 故此行手写.)
root_usage_turn = json.dumps({"type": "assistant", "timestamp": "2026-06-17T10:00:00+08:00",
                              "message": {"role": "assistant",
                                          "usage": {"input_tokens": 3000, "cache_creation_input_tokens": 0,
                                                    "cache_read_input_tokens": 6000, "output_tokens": 100}}},
                             ensure_ascii=False)
# SID_A: root usage turn (ctx 9000) + 一个 spawn (验证 ctxPeak 通道与 spawn 管线并存不污染)
build_session(d, "ctxproj", SID_A, [root_usage_turn, agent_line("a1", total_tokens=1000)])
# SID_B 对照: 只 spawn + 纯 user 行 (无 root usage) → ctxPeak==0 但字段在
build_session(d, "ctxproj", SID_B, [agent_line("a2", total_tokens=2000),
                                     json.dumps({"type": "user", "message": {"role": "user"}}, ensure_ascii=False)])
res = run_scan(d)
check("exit 0", res.get("_rc") == 0, res.get("_stderr"))
check("perSession 长度=2", len(res.get("perSession", [])) == 2, res.get("perSession"))
rows = {r["sid"]: r for r in res["perSession"]}
check("SID_A ctxPeak==9000 (root turn 3000+6000; spawn 行不计 root ctx)",
      rows[SID_A].get("ctxPeak") == 9000, rows.get(SID_A))
check("SID_B ctxPeak==0 (无 root usage, 字段在)", rows[SID_B].get("ctxPeak") == 0, rows.get(SID_B))
shutil.rmtree(d, ignore_errors=True)


# ===== S14 Phase 3 跨 session 缝合 (generations.jsonl 映射两 sid → 同 gid; result.generations multiSession) =====
print("\n[S14] 跨 session 缝合 — generations.jsonl 映射两 sid 到同 g-task → 一条 multiSession generation")
d = tempfile.mkdtemp(prefix="obs-scan-s14-")
build_session(d, "fleet", SID_A, [agent_line("a1", total_tokens=1000)])
build_session(d, "fleet", SID_B, [agent_line("a2", total_tokens=2000)])
# generations.jsonl 放 <log_base> 根 (base, 非 scan_dir; 跨 project 全局表) — 两 sid → 同 g-task
log_base = tempfile.mkdtemp(prefix="obs-scan-s14-glb-")
with open(os.path.join(log_base, "generations.jsonl"), "w") as f:
    f.write(json.dumps({"schemaVersion": 1, "recordType": "GenerationLineage",
                        "sessionId": SID_A, "generationId": "g-task", "carrierSource": "env",
                        "source": "startup", "writer": "plugin-hook", "projectName": "fleet"}) + "\n")
    f.write(json.dumps({"schemaVersion": 1, "recordType": "GenerationLineage",
                        "sessionId": SID_B, "generationId": "g-task", "carrierSource": "env",
                        "source": "resume", "writer": "plugin-hook", "projectName": "fleet"}) + "\n")
env = {**os.environ, "AGENTINSIGHT_LOG_DIR": log_base}   # 继承 PATH/HOME/locale, 只覆写 LOG_DIR
res = run_scan(d, env=env)
check("exit 0", res.get("_rc") == 0, res.get("_stderr"))
check("--json 含 generations (新可见产物)", "generations" in res, list(res.keys()))
ps = res.get("perSession", [])
check("perSession=2", len(ps) == 2, len(ps))
check("两行 generationId 都=g-task (map post-ingest 覆盖)",
      all(r.get("generationId") == "g-task" for r in ps), [r.get("generationId") for r in ps])
gens = res.get("generations", [])
check("generations 列表非空", isinstance(gens, list) and len(gens) >= 1, gens)
multi = [g for g in gens if g.get("multiSession")]
check("恰好 1 条 multiSession generation", len(multi) == 1, [g.get("generationId") for g in gens])
mg = multi[0] if multi else {}
check("multiSession generationId=g-task", mg.get("generationId") == "g-task", mg)
check("multiSession sessionsN=2", mg.get("sessionsN") == 2, mg)
check("multiSession sessionIds=[A,B]", set(mg.get("sessionIds", [])) == {SID_A, SID_B}, mg)
# 卷起 grandTotal.total == 两成员 session perSession grandTotal.total 之和 (口径无关, 只验求和算术)
expected_total = sum(r.get("grandTotal", {}).get("total", 0) for r in ps)
check("multiSession grandTotal.total == 成员 session 之和 (卷起)",
      mg.get("grandTotal", {}).get("total") == expected_total,
      {"gen": mg.get("grandTotal"), "sum": expected_total})
shutil.rmtree(d, ignore_errors=True)
shutil.rmtree(log_base, ignore_errors=True)

# ===== S15 Phase 3 foreign session 回退 (map 缺一 → 后者 singleton generationId==sid, 不崩) =====
print("\n[S15] foreign 回退 — map 有 SID_A 缺 SID_B → B generationId==sid 自成 singleton, 不崩")
d = tempfile.mkdtemp(prefix="obs-scan-s15-")
build_session(d, "fleet", SID_A, [agent_line("a1", total_tokens=1000)])
build_session(d, "fleet", SID_B, [agent_line("a2", total_tokens=2000)])
log_base = tempfile.mkdtemp(prefix="obs-scan-s15-glb-")
with open(os.path.join(log_base, "generations.jsonl"), "w") as f:
    # 只 SID_A 入 map; SID_B 缺 → reader 回退 generationId=sessionId
    f.write(json.dumps({"schemaVersion": 1, "recordType": "GenerationLineage",
                        "sessionId": SID_A, "generationId": "g-known", "carrierSource": "env",
                        "source": "startup", "writer": "plugin-hook"}) + "\n")
env = {**os.environ, "AGENTINSIGHT_LOG_DIR": log_base}
res = run_scan(d, env=env)
check("exit 0 (foreign 不崩)", res.get("_rc") == 0, res.get("_stderr"))
ps_map = {r["sid"]: r for r in res.get("perSession", [])}
check("SID_A generationId=g-known (命中 map)", ps_map.get(SID_A, {}).get("generationId") == "g-known",
      ps_map.get(SID_A, {}).get("generationId"))
check("SID_B generationId==SID_B (未命中 → sid 回退)",
      ps_map.get(SID_B, {}).get("generationId") == SID_B, ps_map.get(SID_B, {}).get("generationId"))
gens = res.get("generations", [])
by_gid = {g.get("generationId"): g for g in gens}
check("generations 2 条 (g-known + SID_B 各一)", len(gens) == 2, [g.get("generationId") for g in gens])
check("g-known generation singleton (multiSession=False, 只 SID_A)",
      by_gid.get("g-known", {}).get("multiSession") is False, by_gid.get("g-known"))
check("SID_B 自成 singleton generation (multiSession=False)",
      by_gid.get(SID_B, {}).get("multiSession") is False, by_gid.get(SID_B))
shutil.rmtree(d, ignore_errors=True)
shutil.rmtree(log_base, ignore_errors=True)

# ===== S16 Phase 3 inert (无 generations.jsonl → 全 singleton, 今天行为逐字不变) =====
print("\n[S16] inert — log_base 无 generations.jsonl → 空 map → perSession generationId==sid; generations 全 singleton")
d = tempfile.mkdtemp(prefix="obs-scan-s16-")
build_session(d, "fleet", SID_A, [agent_line("a1", total_tokens=1000)])
build_session(d, "fleet", SID_B, [agent_line("a2", total_tokens=2000)])
log_base = tempfile.mkdtemp(prefix="obs-scan-s16-glb-")   # 空 (刻意不建 generations.jsonl)
env = {**os.environ, "AGENTINSIGHT_LOG_DIR": log_base}
res = run_scan(d, env=env)
check("exit 0 (inert)", res.get("_rc") == 0, res.get("_stderr"))
ps = res.get("perSession", [])
check("inert: perSession 每行 generationId==sid (空 map 不覆盖)",
      all(r.get("generationId") == r.get("sid") for r in ps), [(r.get("sid"), r.get("generationId")) for r in ps])
gens = res.get("generations", [])
check("inert: generations 全 singleton (multiSession=False)",
      all(not g.get("multiSession") for g in gens), [g.get("multiSession") for g in gens])
check("inert: generations 2 条 (每 sid 各一 singleton)", len(gens) == 2, len(gens))
shutil.rmtree(d, ignore_errors=True)
shutil.rmtree(log_base, ignore_errors=True)


# ===== 收尾 =====
print("\n" + "=" * 70)
print(f"结果: {passed} PASS / {failed} FAIL")
print("验证范围: Mode B · scan-projects (discover → 逐 session load_transcript → 跨 session 聚合, 隔离合成).")
print("平台边界 (§9.3): Mode B 恒 depth-2; depth-3 须 live hook. Mode B 一致 by construction.")
print("=" * 70)
sys.exit(1 if failed else 0)
