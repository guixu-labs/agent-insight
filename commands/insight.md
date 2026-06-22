---
description: agent-insight 主动入口 — 当前 session 编排摘要 + dashboard URL（子命令 live / scan / session）
argument-hint: "[live | scan | session <id>]"
---

# /insight — agent-insight 主动查询入口

本插件默认**被动**(只有 hook)。`/insight` 补上主动查询:跑 reader `tools/analyze.py` 把结果摘要进 chat,并给 dashboard URL。**只读、零耦合**——不注册 hook、不改编排。

按用户参数 `$ARGUMENTS` 分流(无参数 = 默认)。

## 🔴 执行红线(每次必守)

- **只读**:只跑 reader(`tools/analyze.py`)或启 dashboard server(passive),**绝不注册 hook、绝不 cat `~/.claude/settings.json`、绝改编排**。
- **绝对路径**:对 `~/.claude/projects` 的任何 `find`/`grep` 必须绝对路径 + `--`(CC project 目录名 `-home-...` 会被当选项吞)。
- **dashboard 重启用绝对路径**启动 server(Bash cwd 在 calls 间持久,相对路径前缀重复致 `python` Exit 2)。
- **验证而非断言**:贴 reader 的真实数字,不空口"大概花了多少 token"。

## 通用:project 目录名约定

CC 把 session transcript 存在 `${AGENTINSIGHT_PROJECTS_ROOT:-~/.claude/projects}/<projdir>/<sid>.jsonl`,其中 `<projdir>` = 该 session 启动时 cwd 的绝对路径把 `/` 换成 `-`(例:cwd `/home/qwren/agent-insight` → `-home-qwren-agent-insight`)。live JSONL 存在 `${AGENTINSIGHT_LOG_DIR:-~/.claude/agent-insight}/<projname>/<YYYY-MM-DD>.jsonl`,`<projname>` 默认 = cwd 的 basename。

---

## 无参数(默认)—— 当前 session 编排摘要

1. **先试 Mode A**(本插件 hook 落的 live JSONL,完整深度):
   ```bash
   cd /path/to/agent-insight  # 绝对路径
   python3 tools/analyze.py --project "$(basename "$PWD")" --json
   ```
2. **若 `recordsTotal=0`**(hook 没注册 / 当前 session 没落盘)→ 降级 **Mode B**(读当前 CC session transcript,depth-2,平台边界):
   ```bash
   PROJDIR="${AGENTINSIGHT_PROJECTS_ROOT:-$HOME/.claude/projects}/-$(echo "$PWD" | sed 's:^/::; s:/:-:g')"
   SID=$(find -- "$PROJDIR" -maxdepth 1 -name '*.jsonl' -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2)
   python3 tools/analyze.py --transcript "$SID" --json
   ```
   并提示用户:live hook 未注册(见 README「红线:启用必须切 New Session」),当前是 depth-2 事后视图;depth-3+ 嵌套 token 须挂 hook(Mode A)。
3. **从 result JSON 摘要进 chat**(精炼,别贴整块 JSON):
   - **总 token** `grandTotal.total` + **cache 命中率** `cacheRead/(cacheRead+input+cacheCreation)`(output 不进分母);
   - **spawn 数** + 按 `subagentType` 的 top 3(按 `total` 降序,带 calls / avgDur / successRate);
   - **异常信号**:💥 ctx 爆掉 / ⚠ 低命中(<60%) / ⏳ 异步未回报(有则点名);
   - **depth 提示**:Mode A 标"完整深度(含 depth-3+ 嵌套归因)",Mode B 标"depth-2(CC transcript 平台边界)"。
4. **给 dashboard URL**:`http://127.0.0.1:${AGENTINSIGHT_PORT:-8765}`。若 server 未跑,提示 `python3 dashboard/server.py` 起(或问用户要不要现在起)。

---

## `live` —— 切 live 源 tail

1. 确保 dashboard server 在跑;未跑则用绝对路径启:`python3 /path/to/agent-insight/dashboard/server.py --source live`。
2. 告知 URL + 说明 **chip 三态**(刷新轴 × 活性轴):live-tail 开 + 文件在动(最新更新 ≤300s)→ `● 实时`;开但旧 session 长期不动 → `⏳ 静止`;关 → `⏸ 暂停`。
3. 提示:**depth-3+ 嵌套 token 须本 session 挂了 hook 才有**(Mode A);没挂则 live 源也只有 depth-2。

---

## `scan` —— fleet 扫描汇总

1. `python3 /path/to/agent-insight/tools/analyze.py --scan-projects --json`(扫全 `${AGENTINSIGHT_PROJECTS_ROOT:-~/.claude/projects}`)。
2. 摘要:跨 session 合计 token + cache 命中 + session 数 / spawn 数 + **top 5 outlier session**(按 `totalTokens` 降序,带 hit%) + 全局自洽(`consistent` / `errors` 数)。scan 恒 depth-2(Mode B 平台边界)。

---

## `session <id>` —— 钻取单 session

1. 定位 transcript(find 绝对路径 + `--`):
   ```bash
   PROJDIR="${AGENTINSIGHT_PROJECTS_ROOT:-$HOME/.claude/projects}/-$(echo "$PWD" | sed 's:^/::; s:/:-:g')"
   find -- "$PROJDIR" -maxdepth 1 -name "<id>*.jsonl" 2>/dev/null
   ```
2. `python3 /path/to/agent-insight/tools/analyze.py --transcript <path> --tree --json`。
3. 摘要:该 session 的 **call chains**(含 depth / orphan 标记)+ **call graph**(边 × 次数)+ 自洽 + per-subagent token 表 + 异常信号。depth-2(Mode B)。
