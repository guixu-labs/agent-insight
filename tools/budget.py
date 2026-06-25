"""预算判定 + per-session 实时累计(单一源头,record.py + analyze.py 共用).

- _budget_threshold(): 读 env AGENTINSIGHT_BUDGET_THRESHOLD(opt-in 总开关).
- _budget_state(cumulative, threshold): 判定 → {threshold, cumulativeTotal, pctOfThreshold, exceeded} 或 None.
- _session_cumulative(log_base, project, session_id): per-session 实时累计 — 读 project JSONL,
  按 sessionId 过滤 SubagentCall,四桶求和(cacheRead-inclusive,红线 6,口径同 analyze.grand_total).

为何共享(2026-06-24):budget 判定逻辑原在 analyze.py(离线 reader-computes,给人/dashboard 事后回顾
「工作流总共花了多少」);实时事件 emission(对接外部动作层)要 recorder(hook)在线算「单 session 跑到阈值没」→
抽此模块,reader 离线 + recorder 实时共调一个 = 单一源头,防漂移(同 terminal_stats.py 先例).

口径区分(别混):
  - per-session 实时(本模块 _session_cumulative):给外部 handoff 动作层,单 session 阈值触发(本 session 累计).
  - 跨 session 离线(analyze.aggregate_generations + _budget_state):给 dashboard 回顾,generationId 卷起总账.
  两者共用 _budget_state 判定函数,但累计口径不同(per-session vs 跨 session).

红线:recorder 调用路径异常须 swallow(record.py 兜);本模块纯函数 + IO swallow,永不抛.
"""
import glob
import json
import os
from datetime import timezone, timedelta

_TZ = timezone(timedelta(hours=8))   # 本地时区(与 record.py / analyze.py 一致)
_TOK_KEYS = ["input", "output", "cacheCreation", "cacheRead", "total"]


def _budget_threshold():
    """读 AGENTINSIGHT_BUDGET_THRESHOLD env(token int, opt-in). 空/非数字/0 → None(inert,不算预算).

    0 → None(零阈值无意义;下游 _budget_state 亦以 truthiness 复核,双保险).
    镜像既有 AGENTINSIGHT_* 读取模式."""
    raw = os.environ.get("AGENTINSIGHT_BUDGET_THRESHOLD", "").strip()
    if not raw:
        return None
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return None
    return val or None


def _budget_state(cumulative, threshold):
    """预算判定(单一源头;reader 离线 + recorder 实时共调).

    cumulative = total(int); threshold=None/0 → None(inert:不加 budgetState,逐字今天行为).
    pct 取整镜像 cacheReadPct 口径;exceeded = 到阈即超(cumulative >= threshold)."""
    if not threshold:
        return None
    return {
        "threshold": threshold,
        "cumulativeTotal": cumulative,
        "pctOfThreshold": round(cumulative / threshold * 100, 1),
        "exceeded": cumulative >= threshold,
    }


def _tok(t, k):
    """安全取数:非 int/float → 0(镜像 analyze._tok)."""
    v = (t or {}).get(k)
    return v if isinstance(v, (int, float)) else 0


def _session_cumulative(log_base, project, session_id):
    """per-session 实时累计:读 <log_base>/<project>/*.jsonl,按 sessionId 过滤 SubagentCall,
    四桶求和(cacheRead-inclusive,红线 6,口径同 analyze.grand_total)→ {input,output,cacheCreation,cacheRead,total}.

    跨天 session(跨午夜)兼容:扫 project 全部日期文件按 sessionId 过滤(sessionId 全局唯一,不串).
    无文件/缺参/异常 → 全 0 dict(inert).永不抛(recorder 调用红线).

    性能:recorder 每次 Agent hook 都扫 project 全部 JSONL.多数 project 文件少(每天一个,跑几天就几个),
    可接受;超大 project(几十天)可后续优化只扫近 N 天 — v1 简单扫全."""
    agg = {k: 0 for k in _TOK_KEYS}
    if not (log_base and project and session_id):
        return agg
    d = os.path.join(log_base, project)
    try:
        for path in sorted(glob.glob(os.path.join(d, "*.jsonl"))):
            if os.path.basename(path) == "budget-events.jsonl":
                continue   # 别把自己 emit 的 budget-events 当 token 源
            try:
                with open(path) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            r = json.loads(line)
                        except Exception:
                            continue
                        if not isinstance(r, dict):
                            continue
                        if r.get("recordType") != "SubagentCall":
                            continue
                        if r.get("sessionId") != session_id:
                            continue
                        t = r.get("tokens") or {}
                        for k in ("input", "output", "cacheCreation", "cacheRead"):
                            agg[k] += _tok(t, k)
            except Exception:
                continue
    except Exception:
        pass
    agg["total"] = agg["input"] + agg["output"] + agg["cacheCreation"] + agg["cacheRead"]
    return agg
