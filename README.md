# agent-insight

**通用 agent / subagent 编排可观测性工具。** 被动 `PostToolUse` hook 记录每个 subagent 的 **token / 时延 / 成败 / Skill 使用 / 命令结果**,落滚动 JSONL;自带 reader(读 live JSONL 或 CC 原生 transcript,统一 Event IR)与浏览器 dashboard。**opt-in、零耦合、只量不动。**

给 Claude Code 的多 agent 编排一个诚实的账本:谁派了谁、花了多少 token、哪些 skill 被加载、哪条命令失败。**深而精**——完整嵌套深度(depth-3+)的 per-subagent token 归因 + spawn 调用拓扑 + 单一计费核(cache 命中率按计费口径,skill 零 token 不稀释)。

> 本 README 自包含(架构 / 用法 / 状态)。agent 工作约定见 [`AGENTS.md`](AGENTS.md)。

## 它解决什么

Claude Code 跑一个大任务会派出一堆 subagent,但你看不到:

- 这一轮编排总共烧了多少 token,哪个 subagent 最贵;
- subagent 之间怎么嵌套(root → 外层 → 内层),拓扑长啥样;
- cache 命中率多少(省没省钱),哪个 session fresh input 太多该优化;
- 哪个 session 撞了 context window、哪些 spawn 异步起飞还没回报;
- 哪些 skill 被加载、哪些 Bash/命令失败了。

`agent-insight` 被动收割这些事实、落盘、还原成可查的账。不改你的编排、不主动发事件、永不阻断(异常静默丢这一条,编排照跑)。

## 当前状态(v0.1.0)

| 能力 | 状态 |
|---|---|
| **recorder**(三轨 hook → 滚动 JSONL) | ✅ live fire 闭环(2026-06-16) |
| **reader Mode A**(读自有 JSONL,完整 depth 含 depth-3+) | ✅ 已交付 |
| **reader Mode B**(读 CC transcript,depth-2,平台边界) | ✅ 已交付 |
| **`--scan-projects` fleet 扫描**(扫全 `~/.claude/projects/`) | ✅ 已交付 |
| **dashboard Level ① fleet**(hero cache + fleet 表 + by-skill + topology + trust 闸) | ✅ 已交付 |
| **live 源 + live-tail**(mtime-poll 自动 refresh + 前端 2s 轮询) | ✅ 已交付 |
| **运行时数据源切换器**(不重启 server 切 scan/live/自定义) | ✅ 已交付 |
| **深色/浅色主题切换**(右上按钮 · 记住选择) | ✅ 已交付(默认深色 · GitHub Light 同源 · localStorage 记忆) |
| **`/insight` 主动入口**(slash command) | 🔧 待加(见 [commands/](commands/)) |
| Level ② session 编排视图 + hero context 半边 | ✅ 已交付(点行 → 单 session → spawn/turn;hero 双面板含 root ctx 峰值) |
| 跨 session 续接(SessionStart hook + lineage 缝合) | ✅ 已交付(lineage 缝合建满;budgetState defer) |

**真数据基线**(fleet 扫描实测):**17 session / 705 spawn / 41.05M token(91.2% cacheRead)/ all consistent / 0 error**——跨多个真实 CC 编排项目。

## 🔴 红线:启用必须切 New Session

**千万不要在当前开发 session 里启用本插件。** CC hook 配置 **mid-session 不重载**——"注册"(写 settings.json hooks 块 / enable plugin)**不会**立即在当前 session 起火(CC 在 session start 读一次配置,要重启才生效)。三条红线:

- `~/.claude/settings.json` 可能含 auth token,**绝不在里面直接挂 hook**——用项目级 `.claude/settings.local.json`;
- 当前开发 session 须保持干净可重复(注册改的是重启后行为 + 要重启才生效 = 打断开发);
- live 验收应在隔离 New Session 做以可控复现 / 回退。

**启用流程(在 New Session 做)**:

1. 项目级 `.claude/settings.local.json` 手挂 `PostToolUse` hooks 块(`command` 用 `hooks/record.py` 绝对路径),或走 `/plugin install` 装本插件(见下「安装」)。
2. **重启 CC**(新进程读到配置)→ 派任意子 agent 做轻量验收(trivial "pong" 探针即可,无需特定运行时)。
3. 查 `~/.claude/agent-insight/<project>/<date>.jsonl` 真落盘 + `tokens` 非 null = 头号命门 live 立住。

> ⚠️ 若仓库 `.claude/settings.local.json` 里**已挂着** hook 配置,该仓 dev session **就不是 inert**——Agent 调用会真 fire、落盘。开发期宜移除配置,或明知只量不动。

## 三条捕获轨道

| 轨道 | hook | recordType | 默认 | 价值 |
|---|---|---|---|---|
| **Agent** | `PostToolUse(Agent)` | `SubagentCall` | always-on | per-subagent token / 时延 / 成败(核心) |
| **Skill** | `PostToolUse(Skill)` | `SkillCall` | always-on | 哪个 subagent 加载了哪些 capability skill(零 token,不入 grandTotal) |
| **Bash** | `PostToolUse(Bash)` | `Command` | **opt-in 默认关** | verify / 校验命令的 `interrupted` + `stderr` |

Bash 高频 × fork-exec 会拖慢编排,故默认关、`AGENTINSIGHT_BASH=1` 才开——v1 唯一被 opt-in 限流的功能,其余都在低频 Agent/Skill 轨道、always-on 无感。

## 配置(环境变量)

全部 `AGENTINSIGHT_*` 前缀。未设则用默认,零配置即可跑。

| env | 作用 | 默认 |
|---|---|---|
| `AGENTINSIGHT_LOG_DIR` | JSONL 根目录 | `~/.claude/agent-insight` |
| `AGENTINSIGHT_PROJECT` | project 子目录名(分组) | cwd 的 basename |
| `AGENTINSIGHT_BASH` | 开 Bash 轨道(`1`/`true`/`yes`) | 关 |
| `AGENTINSIGHT_BROWSE_ROOT` | dashboard `/api/browse` 目录浏览弹层的可信根 | `~`(home) |
| `AGENTINSIGHT_PORT` | dashboard 端口 | `8765` |
| `AGENTINSIGHT_SOURCE` | dashboard 初始数据源(可被 `--source` 覆盖) | `scan` |
| `AGENTINSIGHT_PROJECTS_ROOT` | fleet 扫描根目录 | `~/.claude/projects` |
| `AGENTINSIGHT_CARRIER_ID` | 跨 session 续接 carrier(env 通路) | 无 → `generationId` 退化成 `sessionId` |
| `AGENTINSIGHT_CARRIER_FILE` | 续接 carrier(handoff 文件通路) | 无 |

## 输出

滚动 JSONL,按天分文件、按 project 分目录:

```
<logDir>/<project>/YYYY-MM-DD.jsonl
```

每行一条 record,三种形态(`recordType`)详见 [`schema/subagent-call.schema.json`](schema/subagent-call.schema.json)。在线 recorder 只落**原始事实**(`caller` / `spawned` / `tokens`);`parentType` / `callChain` 是 reader 从 `caller ↔ spawned agentId` 匹配派生的视图(无状态)。

**拓扑归属**:每条事件自带显式 caller→spawned 链接——`caller.agentId` = 顶层 `agent_id`(缺失 = root 直发)、`spawned.agentId` = `tool_response.agentId`。并发多波也精确(每条点名 caller,不靠时序)。

## 安装

两条路,按场景选:

**1. Claude Code 插件(主路径)** —— `/plugin marketplace add guixu-labs/agent-insight` → `/plugin install agent-insight`。重启 CC 生效。hook 自动挂好。

**2. 零依赖 clone(stdlib only)** —— `git clone` 后直接 `python3 dashboard/server.py` 或 `python3 tools/analyze.py`。纯标准库,无 pip install。

## reader(`tools/analyze.py` · Mode A + Mode B)

把落盘的 JSONL / CC transcript 读回来,还原 token 账 + 调用拓扑 + 自洽诊断——无需复跑、零耦合。

### 怎么选:Mode A 还是 Mode B?

**一句话判据**:那个 session **挂过本插件 hook**(record.py 落过盘)→ Mode A(完整 depth,含 depth-3+ 嵌套 token);**没挂过 / 别人的 / 装插件前的旧 session** → 只能 Mode B(depth-2,CC transcript 不持久化嵌套结构化 spawn,平台边界)。两者产**同样的 Event IR / 同样的输出格式**,差别只在嵌套深度。

| 你有的数据 | 在哪 | Mode | 深度 |
|---|---|---|---|
| 本插件 hook 落的 JSONL | `~/.claude/agent-insight/<project>/<date>.jsonl` | **A** | 完整(含 depth-3+ 真嵌套) |
| CC 原生 transcript(所有 session 都有) | `~/.claude/projects/<project>/<sid>.jsonl` | **B** | depth-2 only |

```bash
# 扫默认 logdir 下全部 project(最常用)
python3 tools/analyze.py

# 只看某个 project / 某天起 / 单文件
python3 tools/analyze.py --project my-project
python3 tools/analyze.py --since 2026-06-16
python3 tools/analyze.py --jsonl ~/.claude/agent-insight/my-project/2026-06-16.jsonl

# 逐条调用链(depth / parentType / orphan 标记)
python3 tools/analyze.py --tree

# 机器可读(下游 / dashboard / CI)
python3 tools/analyze.py --json

# C 形态 live-tail(CLI):2s 轮询 · mtime 变才清屏重印 · Ctrl-C 退出
python3 tools/analyze.py --watch
```

输出三块:

- **token 账**:grandTotal(input/output/cacheCreation/cacheRead/total)+ 按 `subagentType` 聚合(calls/total/avgDur/successRate,按 total 降序);
- **调用拓扑**:call graph(`parentType → childType` 边 × 触发次数);`--tree` 再给逐条 `callChain`(按 sessionId 分组、`agent_id` 离线链接,达根前置 `orchestrator` 角色标签);
- **自洽诊断**:`isRoot` 不变量交叉校验、orphan caller(caller 未在本 session 捕获 → 嵌套内层未录,非一致性违例)、null spawned / null tokens 注记。

### Mode B — 喂 CC transcript(绕过 hook)

没开本插件 hook 的 session(别人跑过 / 旧 session),只要留有 CC 原始 transcript,也能离线还原 per-subagent token + 拓扑——无需 hook、无需复跑。

```bash
# 喂一个 root session transcript(自动探同级 <sid>/subagents/agent-*.jsonl)
python3 tools/analyze.py --transcript ~/.claude/projects/<proj>/<sid>.jsonl --json

# 喂整个 session 目录(root .jsonl + subagents/)
python3 tools/analyze.py --transcript ~/.claude/projects/<proj>/<sid>/ --tree
```

复用 Mode A 同一条管线,只换 ingest 入口(`tools/transcript_adapter.py` 解析 transcript 的 `toolUseResult`)。输出格式与 Mode A 完全一致。

**🔴 平台硬边界**:CC transcript **只对 root 直发 Agent 调用持久化结构化 `toolUseResult`**;嵌套调用(子 agent 再派子 agent)只落成 message content 文本块、该行 `toolUseResult=null` → **Mode B 只重建 depth-2(root→agent),depth-3+ token / 拓扑须靠 live hook**。三独立真实样本验证全 depth-2、零嵌套。

### Mode B · fleet — 扫整个 projects 目录(`--scan-projects`)

一把梭出全部历史编排 session 的**聚合 fleet 报告**:per-session 汇总 + 跨 session 合计 + top outlier + scan 级自洽 + depth-2 banner。纯离线、免 hook——对当前 session 零影响。

```bash
# 裸 flag → 扫全量 fleet (~/.claude/projects)
python3 tools/analyze.py --scan-projects

# 只扫某个 project 子目录
python3 tools/analyze.py --scan-projects ~/.claude/projects/-home-user-myproject

# 机器可读
python3 tools/analyze.py --scan-projects --json
```

逐 session 跑同一条 Mode B 管线(**per-session 错误隔离**:单 session 异常进 `errors[]`、scan 永不 `exit 2`、其余照扫),再跨 session 合并。**合并正确性**:`avgDurationMs`/`successRate` 是率,**用原始累加器(`durSum`/`durN`/`successCount`)重算,不平均平均**。

## dashboard(浏览器 · Level ① fleet)

薄 stdlib HTTP server 喂 `analyze.py --json` 产物,静态 HTML/JS 渲染 fleet 总览(hero cache 半边 + fleet 表 + by-skill 切面 + topology + trust 闸)。零外部依赖、资产 vendored 无 CDN。

**深色/浅色主题**:右上角 ☀️ / 🌙 按钮切换。**默认深色**;切到浅色(GitHub Light 同源调色)后选择记在浏览器 `localStorage`,刷新记住——不跟随系统。清掉 `localStorage` 的 `ai-theme` 即恢复默认深色。

```bash
# 默认 scan 源(扫 ~/.claude/projects)
python3 dashboard/server.py
# 浏览器开 http://127.0.0.1:${AGENTINSIGHT_PORT:-8765}

# 指定数据源 / 端口
python3 dashboard/server.py --source scan:~/.claude/projects/-home-user-myproject --port 9000
AGENTINSIGHT_PORT=9000 python3 dashboard/server.py --source transcript:~/.claude/projects/<proj>/<sid>.jsonl

# 用 env 固定初始源(部署 / systemd / 容器)
AGENTINSIGHT_SOURCE=live python3 dashboard/server.py
```

数据源(`--source` / `AGENTINSIGHT_SOURCE`):`scan`(默认)/ `scan:DIR` / `transcript:PATH` / `jsonl:PATH` / `file:PATH`(直读 result JSON 快照)/ `live`(live logdir)/ `live:DIR`。**裸路径自动识别**:路径框直接填目录或 `.jsonl` 路径(不加前缀),server 自动推断(目录 → `scan:`,在 live logdir 基下则 `live:`;`.jsonl` → `transcript:`;其他 → 400)。

### 三条正交轴(别混)

- **来源轴**——数据从哪来:live(record.py hook 实时编排日志、完整嵌套深度)/ scan·transcript(CC 原生 transcript、depth-2)。显在「数据源」下拉,**不在 chip**。
- **刷新轴**——页面多久重拉:live-tail 开(前端 2s 轮询 + 服务端 mtime-poll 重算)/ 关(冻结)。
- **活性轴**——数据在不在动:文件最新更新距今 ≤ 300s 视为在动。

**mode chip 三态 = 刷新轴 × 活性轴**:live-tail 关 → `⏸ 暂停`;开 且在动 → `● 实时`;开 但长期不动(旧 session)→ `⏳ 静止`(文件恢复更新下次轮询即回 `● 实时`)。chip 与来源正交:scan 源 + live-tail 开 + 有新 session 落盘 → chip 显 `● 实时`(不是「离线」)。

### 页面元素速查

- **① banner** `✓ 0 异常 · N sessions · N spawns` —— 三类信号各数一遍:💥 上下文爆掉、⚠ 低命中(<60%)、⏳ 异步未回报。全 0 绿;任一 >0 拆红段。
- **② 健康列**(fleet 表最后一列图标)—— 每 session 行取最严重信号显一个图标:💥 > ⚠ > ⏳ > ✓。同时是排序键,问题 session 自动沉顶。
- **③ mode chip** —— 见上(刷新×活性三态)。
- **④ ✗ tool 失败** —— 故意不上 fleet,只在详情页。Bash 非零退出、Edit 未命中等 `is_error` 下沉到出问题的 root/spawn/turn 行。

## `/insight`(规划中)

主动入口 slash command(待加 [commands/](commands/)):

- `/insight` —— 当前 session 编排摘要 in-chat + dashboard localhost URL;
- `/insight live` —— 切 live 源 tail;
- `/insight session <id>` —— 钻取单 session;
- `/insight scan` —— 跑一次 fleet 扫描汇总。

当前插件是**被动**的(只有 hook,无 command);`/insight` 补上主动查询入口。

## 测试

```
python3 tests/test_record.py              # recorder 落盘逻辑 (73/73)
python3 tests/test_analyze.py             # Mode A reader 拓扑 + 自洽 + live-tail --watch (144/144)
python3 tests/test_transcript_adapter.py  # Mode B transcript ingest (83/83)
python3 tests/test_scan_projects.py       # Mode B fleet 扫描 + 跨 session 合并 (102/102)
python3 tests/test_dashboard.py           # dashboard server 契约 + scaffolding + live 源/切换器/浏览弹层 (636/636)
```

五套都隔离(子进程 + 临时目录 / env),**不碰真 session / settings.json**。**全绿**(2026-06-23 清掉历史 token 口径债:`grandTotal.total` 统一为四桶求和含 cacheRead,fixture 对齐)。

## 形态与边界

- **独立 plugin**(不是 skill / agent),核心是 `hooks/`。无 `agents/`、无 `workflows/`。
- **零耦合被动观测**:不改编排、不主动发事件,只挂全局 `PostToolUse` 收割事件流。唯一破"零耦合"处是跨 session 续接的 carrier + lineage 约定(可选,未设则退化)。
- **只量不动**:recorder 永不阻断编排(异常 swallow + `exit 0`)。
- **v1 平台范围**:只锁 Claude Code target。

## 许可

MIT。
