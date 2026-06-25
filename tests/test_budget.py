#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/budget.py 单测:预算判定 + per-session 实时累计(单一源头, 2026-06-24 抽离).

【隔离】不碰真 ~/.claude:_session_cumulative 用 tempfile 造合成 JSONL;env 在单测进程内设/清(进程退即净).
不调 record.py / 不碰 settings.json. 同 test_record 隔离精神.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
import budget   # noqa: E402

passed = 0


def check(name, cond, detail=""):
    global passed
    ok = bool(cond)
    if ok:
        passed += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"   {detail}"))


print("\n[组1] _budget_threshold(env 读取)")
os.environ.pop("AGENTINSIGHT_BUDGET_THRESHOLD", None)
check("空 env → None", budget._budget_threshold() is None)
for val, exp in [("60000", 60000), ("0", None), ("abc", None), ("  5000  ", 5000), ("3.5", None)]:
    os.environ["AGENTINSIGHT_BUDGET_THRESHOLD"] = val
    got = budget._budget_threshold()
    check(f"env={val!r} → {exp}", got == exp, f"got {got!r}")
os.environ.pop("AGENTINSIGHT_BUDGET_THRESHOLD", None)

print("\n[组2] _budget_state(判定逻辑)")
check("threshold None → None", budget._budget_state(100, None) is None)
check("threshold 0 → None", budget._budget_state(100, 0) is None)
s = budget._budget_state(30, 60)
check("30/60 → pct=50, exceeded=False", s and s["pctOfThreshold"] == 50.0 and s["exceeded"] is False, s)
s = budget._budget_state(60, 60)
check("60=60 → exceeded=True(到阈即超), pct=100", s and s["exceeded"] is True and s["pctOfThreshold"] == 100.0, s)
s = budget._budget_state(90, 60)
check("90>60 → exceeded=True, pct=150", s and s["exceeded"] is True and s["pctOfThreshold"] == 150.0, s)
check("cumulativeTotal 回带", s and s["cumulativeTotal"] == 90, s)
check("threshold 回带", s and s["threshold"] == 60, s)

print("\n[组3] _session_cumulative(per-session 实时累计)")
with tempfile.TemporaryDirectory() as td:
    proj = os.path.join(td, "myproj")
    os.makedirs(proj)
    with open(os.path.join(proj, "2026-06-24.jsonl"), "w") as f:
        # session A:2 条 SubagentCall
        f.write(json.dumps({"recordType": "SubagentCall", "sessionId": "A",
                            "tokens": {"input": 100, "output": 20, "cacheCreation": 0, "cacheRead": 80, "total": 200}}) + "\n")
        f.write(json.dumps({"recordType": "SubagentCall", "sessionId": "A",
                            "tokens": {"input": 50, "output": 10, "cacheCreation": 0, "cacheRead": 40, "total": 100}}) + "\n")
        # session B:不该计入 A
        f.write(json.dumps({"recordType": "SubagentCall", "sessionId": "B",
                            "tokens": {"input": 999, "output": 0, "cacheCreation": 0, "cacheRead": 0, "total": 999}}) + "\n")
        # SkillCall(非 SubagentCall)不计
        f.write(json.dumps({"recordType": "SkillCall", "sessionId": "A", "tokens": None}) + "\n")
        # 坏 token 值 → 0(不崩)
        f.write(json.dumps({"recordType": "SubagentCall", "sessionId": "A", "tokens": {"input": "BAD"}}) + "\n")
    # budget-events.jsonl 在 base 根(td),不在 project 子目录 → 自然不被扫;再验不影响
    with open(os.path.join(td, "budget-events.jsonl"), "w") as f:
        f.write(json.dumps({"recordType": "BudgetEvent", "cumulativeTotal": 99999}) + "\n")

    cumA = budget._session_cumulative(td, "myproj", "A")
    check("A input=150(100+50)", cumA["input"] == 150, cumA)
    check("A cacheRead=120(80+40)", cumA["cacheRead"] == 120, cumA)
    check("A total=300(四桶和; cacheRead 计入 红线6)", cumA["total"] == 300, cumA)
    check("B 不串入 A(input≠999)", cumA["input"] != 999)
    cumB = budget._session_cumulative(td, "myproj", "B")
    check("B input=999", cumB["input"] == 999, cumB)
    zero = {"input": 0, "output": 0, "cacheCreation": 0, "cacheRead": 0, "total": 0}
    check("log_base None → 全 0", budget._session_cumulative(None, "myproj", "A") == zero)
    check("project 不存在 → 全 0", budget._session_cumulative(td, "nope", "A") == zero)
    check("sessionId 不存在 → 全 0", budget._session_cumulative(td, "myproj", "ZZZ") == zero)

print(f"\n{'=' * 40}\nbudget 单测:{passed} 通过")
sys.exit(0)
