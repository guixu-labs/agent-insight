#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent-insight 整体 live 自检 — 验 hook → record → reader 链路在真 session 立住。

给「装好插件、想确认到底有没有在正常记编排」的用户/维护者的整体 live 验收工具。
覆盖**必须 New Session 才能验**的平台行为(合成单测验不了,CLAUDE.md 测试纪律留的这一格):
  A. Agent 轨落盘 + token 命门(四桶非 null)        ← 核心
  B. depth-3+ 嵌套捕获 + caller 拓扑                ← 核心 moat(独立成仓后从未 live 验过)
  C. Skill / Bash 轨落盘(条件:跑了对应 probe)
  D. 跨 session lineage 缝合(条件:设了 carrier 跑多 session)
  E. reader 自洽(consistent)

== 用法(三步,在隔离 New Session 做;红线 3/F7:hook mid-session 不重载)==

  1. 挂 hook(见 README「安装」):
       /plugin marketplace add guixu-labs/agent-insight  →  /plugin install agent-insight
     或项目级 .claude/settings.local.json 手挂 PostToolUse hooks 块(command 用 hooks/record.py 绝对路径)。
     → 重启 CC(新进程才读配置)。

  2. python3 tools/live_check.py --show-probe <name>    # 打印触发 prompt,复制喂给 CC
       agent   — Agent 轨基本落盘(派 general-purpose 回 pong)
       nested  — depth-3+ 嵌套(root→A→B→C,验嵌套捕获)
       skill   — Skill 轨(派 subagent 用 skill)
       bash    — Bash 轨(跑条命令;需先 AGENTINSIGHT_BASH=1 启 session)

  3. python3 tools/live_check.py                        # 读落盘 + 全套断言 + verdict

== 性质 ==

  状态报告器(非 pass/fail test):绝不 exit 2(红线 1/7,即便误塞进 hook 链也不阻断编排);
  仅真异常(analyze 崩 / JSON 炸)exit 1。depth<3 / 没录到嵌套不是失败,是有效发现。
  复用 analyze.py --json 全部派生(build_topology / consistency / grand_total),本脚本零算法(红线 10)。
  stdlib only。
"""
import datetime
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ANALYZE = os.environ.get("AGENTINSIGHT_ANALYZE") or os.path.join(HERE, "analyze.py")

# ---------------- 触发 probes(复制喂给 New Session 的 CC)----------------
PROBES = {
    "agent": (
        "请用 Agent 工具派一个 general-purpose 类型的子 agent,prompt 就一句:回复 pong\n"
        "这会触发一次 PostToolUse(Agent),agent-insight 应落一条 SubagentCall(带 token)。"
    ),
    "nested": (
        "你是一个层级派生探针,用来验证 agent-insight 的嵌套捕获。"
        "严格按下面执行,每一层都必须用 Agent 工具(general-purpose 类型)派生下一层,"
        "不要自己直接回答、不要跳层。\n\n"
        "【第 1 层 · 你=root】用 Agent 工具派 1 个 general-purpose 子 agent,"
        "把下面这段话【逐字】作为它的 prompt(不要改写):\n\n"
        "你是层级派生探针的第 2 层(当前层级 2,目标 4 层)。"
        "用 Agent 工具派 1 个 general-purpose 子 agent,把下面这段话【逐字】作为它的 prompt:\n\n"
        "你是层级派生探针的第 3 层(当前层级 3,目标 4 层)。"
        "用 Agent 工具派 1 个 general-purpose 子 agent,把下面这段话【逐字】作为它的 prompt:\n\n"
        "你是层级派生探针的第 4 层(叶子层)。不要再派生任何子 agent。直接回复一个词:pong\n\n"
        "收到第 3 层子 agent 的回复后,原样返回给派你的人。\n"
        "收到第 2 层子 agent 的回复后,原样返回给派你的人。\n\n"
        "【第 1 层(root)】收到第 2 层的返回后,告诉我\"层级派生探针完成,共 4 层\"。"
    ),
    "skill": (
        "请用 Agent 工具派一个 general-purpose 子 agent,让它在执行中用 Skill 工具调用任意一个"
        "已安装的 skill(或直接 /某 skill)。若你的环境没有任何 skill,跳过本 probe。\n"
        "skill 调用会触发 PostToolUse(Skill),落一条 SkillCall(零 token,不计入 grandTotal)。"
    ),
    "bash": (
        "请运行一条 Bash 命令(例如:ls)。\n"
        "注意:Bash 轨默认关。必须先用 AGENTINSIGHT_BASH=1 启动本 session,这条命令才会触发 "
        "PostToolUse(Bash) 落一条 Command(带 interrupted/stderr)。"
    ),
}


def show_probe(name):
    if name not in PROBES:
        print(f"未知 probe: {name!r}。可选:{', '.join(PROBES)}", file=sys.stderr)
        sys.exit(1)
    print(f"===== probe: {name} =====(复制下面整段喂给 New Session 的 CC)\n")
    print(PROBES[name])


# ---------------- 定位落盘 JSONL ----------------
def locate_argv():
    """返回传给 analyze.py 的 argv(不含 --json)。三级降级:
    env JSONL → --jsonl;env PROJECT+SINCE → --project --since;默认 cwd basename + 今天。"""
    if os.environ.get("AGENTINSIGHT_LIVECHECK_JSONL"):
        return ["--jsonl", os.environ["AGENTINSIGHT_LIVECHECK_JSONL"]]
    proj = os.environ.get("AGENTINSIGHT_LIVECHECK_PROJECT") or os.path.basename(os.getcwd())
    since = os.environ.get("AGENTINSIGHT_LIVECHECK_SINCE") or datetime.date.today().isoformat()
    return ["--project", proj, "--since", since]


def run_analyze(argv):
    """fork analyze.py --json, 返回 (result_dict, err)。err 非 None = 真异常。"""
    cmd = [sys.executable, ANALYZE] + argv + ["--json"]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        return None, f"analyze.py exit {p.returncode}\nstderr:\n{p.stderr}"
    try:
        return json.loads(p.stdout), None
    except Exception as e:
        return None, f"json 解析失败: {e}\nstdout[:400]: {p.stdout[:400]}"


# ---------------- 断言组 ----------------
def check(res):
    """返回 (rows, verdict)。每行 (name, status, detail);status ∈ PASS/REPORT/FAIL。"""
    rows = []
    by_track = res.get("byTrack", {}) or {}
    chains = res.get("callChains", []) or []
    cons = res.get("consistency", {}) or {}
    gens = res.get("generations", []) or {}
    n_agent = by_track.get("SubagentCall", 0)
    n_skill = by_track.get("SkillCall", 0)
    n_bash = by_track.get("Command", 0)

    if n_agent == 0 and n_skill == 0 and n_bash == 0:
        return rows, "NO_DATA"

    # A1 Agent 轨落盘
    rows.append(("A1 Agent 轨落盘",
                 "PASS" if n_agent >= 1 else "FAIL",
                 f"SubagentCall={n_agent}"))
    # A2 token 命门(consistency.nullTokens == 0 = 所有 SubagentCall 的 token.total 非 null)
    null_tok = cons.get("nullTokens", 0)
    rows.append(("A2 token 命门(四桶非 null)",
                 "PASS" if (n_agent >= 1 and null_tok == 0) else "FAIL",
                 f"nullTokens={null_tok}" + (""
                  if null_tok == 0 else "  ← 有 SubagentCall 的 token.total 为 null(命门不过)")))
    # A3 token 终态补全(capturePhase=complete 比例;诊断:complete=agent 文件终态真值,非末轮低估)
    if n_agent >= 1 and chains:
        complete = sum(1 for c in chains if c.get("capturePhase") == "complete")
        ratio = complete / len(chains)
        rows.append(("A3 token 终态补全(capturePhase=complete)",
                     "PASS" if ratio >= 0.5 else "REPORT",
                     f"{complete}/{len(chains)} = {ratio:.0%}"
                     + ("(complete = agent 文件终态真值)" if ratio >= 0.5
                        else "(低 = 多为末轮低估 token;计费口径仍对,reader reconcile 会补)")))
    # E 自洽
    consistent = cons.get("consistent")
    rows.append(("E  reader 自洽(consistent)",
                 "PASS" if consistent else "FAIL",
                 f"consistent={consistent}"
                 + ("" if consistent else f"  isRootInvariantViolations={cons.get('isRootInvariantViolations')}")))

    core_ok = (n_agent >= 1 and null_tok == 0 and consistent)

    # B depth-3+ 嵌套捕获(moat 专项)
    depths = [c.get("depth", 0) for c in chains]
    max_depth = max(depths) if depths else 0
    deep = [c for c in chains if c.get("depth", 0) >= 3]
    if deep:
        # 嵌套层(depth>=3 = 被 subagent 派生,非 root 直发)caller 正确性:
        # isRoot 应 False + callerAgentId 非 null(否则 CC 嵌套层未透传 agent_id → moat falsify)
        nested_ok = all(c.get("isRoot") is False and c.get("callerAgentId") for c in deep)
        b_status = "PASS" if nested_ok else "FAIL"
        b_detail = (f"max_depth={max_depth}; depth>=3 共 {len(deep)} 条;嵌套层 "
                    + ("caller 正确链接(isRoot=false + callerAgentId 非 null)"
                       if nested_ok else
                       "caller 异常(isRoot 误 true 或 callerAgentId null)→ MOAT_FALSIFIED:"
                       "CC 嵌套层未透传顶层 agent_id,record.py:125 caller.agentId 恒 null"))
        rows.append(("B  depth-3+ 嵌套捕获(moat)", b_status, b_detail))
    elif chains:
        rows.append(("B  depth-3+ 嵌套捕获(moat)", "REPORT",
                     f"max_depth={max_depth}(<3)。跑了 --show-probe nested 吗?LLM 可能没派满 3 层,"
                     "重跑 / 加强 prompt / 换 model。"))
    else:
        rows.append(("B  depth-3+ 嵌套捕获(moat)", "REPORT",
                     "无 callChains(仅 Skill/Bash 轨?)。depth-3+ 专项未测。"))

    # C1 Skill 轨(条件)
    if n_skill >= 1:
        rows.append(("C1 Skill 轨落盘", "PASS", f"SkillCall={n_skill}(零 token,不入 grandTotal)"))
    else:
        rows.append(("C1 Skill 轨落盘", "REPORT", "未测到。跑 --show-probe skill(若环境有 skill)"))
    # C2 Bash 轨(条件)
    if n_bash >= 1:
        rows.append(("C2 Bash 轨落盘", "PASS", f"Command={n_bash}"))
    else:
        rows.append(("C2 Bash 轨落盘", "REPORT",
                     "未测到。需 AGENTINSIGHT_BASH=1 启 session + --show-probe bash"))
    # D lineage(条件)
    multi = [g for g in gens if g.get("multiSession") or (g.get("sessionsN", 1) or 1) >= 2]
    if multi:
        rows.append(("D  跨 session lineage 缝合", "PASS",
                     f"multiSession generation {len(multi)} 条;sessionsN={multi[0].get('sessionsN')}"))
    elif gens:
        rows.append(("D  跨 session lineage 缝合", "REPORT",
                     f"仅单 session generation({len(gens)} 条)。设 AGENTINSIGHT_CARRIER_ID 跨多 session 跑才验缝合"))
    else:
        rows.append(("D  跨 session lineage 缝合", "REPORT", "无 generation 数据"))

    orphan = cons.get("orphanChains", 0)
    if orphan:
        rows.append(("   orphan caller(诊断)", "REPORT",
                     f"{orphan} 条 chain 中途断(caller 未被本 session spawned)— 数据完整性注记,非一致性违例"))

    verdict = "LIVE_OK" if core_ok else "CORE_FAIL"
    return rows, verdict


# ---------------- 输出 ----------------
def report(rows, verdict, res):
    print("=" * 64)
    print("agent-insight live 自检  (hook → record → reader)")
    print("=" * 64)
    if verdict == "NO_DATA":
        proj = os.path.basename(os.getcwd())
        print("\n⚠  NO_DATA — 没落盘任何 Agent/Skill/Bash 记录。")
        print("  最常见:你直接跑了 check,但这个 session 还没派过 subagent。")
        print("  本工具只读已落盘数据 —— 数据要先靠 probe 产生。先跑:")
        print("    python3 tools/live_check.py --show-probe agent")
        print("  拿到 prompt 复制喂给 CC 派个 subagent(触发 hook 落盘),再跑本命令。")
        print("  probe 后仍 NO_DATA,再排查:")
        print("  ① hook 挂了吗(README「安装」:/plugin install 或手挂 settings.local.json)?")
        print("  ② 重启 CC 了吗(F7:hook 配置 mid-session 不重载)?")
        print(f"  ③ project/since 对吗(当前查 project={proj} / 今天)?用 env AGENTINSIGHT_LIVECHECK_JSONL 指定文件")
        print(f"\nVERDICT: NO_DATA   recordsTotal={res.get('recordsTotal')}")
        return
    for name, status, detail in rows:
        tag = {"PASS": "✓", "FAIL": "✗", "REPORT": "•"}[status]
        print(f"  [{tag} {status:6}] {name}: {detail}")
    print("-" * 64)
    b_row = next((r for r in rows if r[0].startswith("B ")), None)
    if verdict == "LIVE_OK":
        print("✅ LIVE_OK — 核心链路在你环境 live 立住(Agent 轨落盘 + token 命门 + 自洽)。")
        if b_row:
            if b_row[1] == "REPORT":
                print("   depth-3+ 嵌套(moat)尚未验到 — 跑 --show-probe nested 补验。")
            elif b_row[1] == "FAIL":
                print("   ⚠ depth-3+ 录到但 caller 异常 = MOAT_FALSIFIED:CC 嵌套层可能未透传 agent_id。")
                print("     这是真平台限制发现(非 bug),带结果回 dev session 决策(carrier 推断层级 / 接受 depth-2 边界)。")
    else:
        print("❌ CORE_FAIL — 核心链路有问题(看上面 ✗ 项):")
        print("   token null = 命门不过(CC payload 与预期不符);consistent=false = reader 不变量违例。")
        print("   带结果回 dev session 排查 hooks/record.py 的字段路径。")
    print(f"\nVERDICT: {verdict}   recordsTotal={res.get('recordsTotal')}   modeLabel={res.get('modeLabel')}")


def main():
    args = sys.argv[1:]
    if args and args[0] in ("-h", "--help"):
        print(__doc__)
        return
    if args and args[0] == "--show-probe":
        if len(args) < 2:
            print(f"用法: --show-probe <{'|'.join(PROBES)}>", file=sys.stderr)
            sys.exit(1)
        show_probe(args[1])
        return
    argv = locate_argv()
    res, err = run_analyze(argv)
    if res is None:
        print(f"\n⚠  ERROR — {err}", file=sys.stderr)
        sys.exit(1)
    try:
        rows, verdict = check(res)
        report(rows, verdict, res)
    except Exception as e:
        print(f"\n⚠  ERROR — 验证逻辑异常: {e}", file=sys.stderr)
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
