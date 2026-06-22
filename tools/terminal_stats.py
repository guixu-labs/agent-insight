"""transcript 终态累计 token 抽取 —— 单一计费口径源 (offline + live 共用).

为何必须单一源 (2026-06-19 实证): live PostToolUse(Agent) 的 tool_response.usage 只携该 subagent
**末轮** API usage (实证: live cacheRead 逐条 1.7x-17x 小于 agent 文件终态累计; async spawn 更是恒 None,
因为 run_in_background 的 PostToolUse 是"已启动"一次性 ack, 完成走 task-notification 另一通道不回写).
离线 transcript_adapter 早先已发现同一缺陷并修正为"agent 文件终态累计". 两路若各写一份逻辑必漂移
→ live/离线 token 不一致 (用户: "难道live计费的token和离线的还不一个数?" 答案: 不该不一致). 故抽此核,
offline (_agent_file_stats) / live (record.py 落盘 + analyze.py _reconcile_live_records 读端补全) 共调一个函数 = 唯一计费口径.

口径 (与 _agent_file_stats 完全一致):
  终态块 (message.stop_reason set) 按 message.id 去重后四桶求和 = 该会话真实累计计费.
  去重理由: CC 一条 assistant message 按内容块 (thinking/text/tool_use) 拆多行, 每行各挂 message.usage;
    只有终态块 (stop_reason set) 携真计费, 中间块带占位全量 input (cr/cc 恒 0). 不去重 → input 虚胖 N×.

bulletproof: 无文件 / 解析异常 / 无终态块 → (None, None). 永不抛 (调用方是 hook, 红线: 观测只量不动).
"""
import json
import os


def terminal_stats(transcript_path):
    """单遍读 transcript JSONL → (model, usage). 唯一计费口径源.

    Args:
        transcript_path: agent-<id>.jsonl 或任意 session transcript 绝对路径.
    Returns:
        (model, usage):
          model = 首条 assistant message.model (resolvedModel 回退源, F6); 无 → None.
          usage = {"input","output","cacheCreation","cacheRead"} 终态块去重四桶求和;
                  无终态块 / 异常 → None (调用方据此回退末轮/占位 usage).
        无文件 → (None, None).
    """
    if not transcript_path or not os.path.isfile(transcript_path):
        return None, None
    model = None
    by_mid = {}   # message_id -> (stop_prio, seq, usage)
    try:
        with open(transcript_path) as f:
            seq = 0
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
                if model is None and msg.get("model"):
                    model = msg["model"]
                usage = msg.get("usage")
                if not isinstance(usage, dict):
                    continue
                seq += 1
                mid = msg.get("id")
                key = mid or ("_noid_" + str(seq))   # 无 id 孤立行各自独立
                prio = 1 if msg.get("stop_reason") is not None else 0
                cur = by_mid.get(key)
                if cur is None or (prio, seq) > (cur[0], cur[1]):
                    by_mid[key] = (prio, seq, usage)
    except Exception:
        return model, None   # model 可能已抽出; usage 放弃 (回退)
    inp = out = cc = cr = 0
    found = False
    for prio, _s, u in by_mid.values():
        if prio > 0:   # 只终态块 (真计费; 中间块占位, 求和会虚胖)
            found = True
            inp += u.get("input_tokens") or 0
            out += u.get("output_tokens") or 0
            cc += u.get("cache_creation_input_tokens") or 0
            cr += u.get("cache_read_input_tokens") or 0
    return model, ({"input": inp, "output": out, "cacheCreation": cc, "cacheRead": cr} if found else None)
