# agent-insight — Agent 工作约定

本工具是**观测基建**,不是编排插件。任何在本目录下工作的 agent 须遵守以下约定。

## 形态

- **独立 plugin**,非 skill / agent。无 `agents/`、无 `workflows/`,核心是 `hooks/`。
- **核心能力已交付**:
  - **recorder**(三轨 hook → 滚动 JSONL)live fire 闭环(2026-06-16);
  - **reader Mode A**(`tools/analyze.py`,读自有 JSONL,完整 depth 含 depth-3+ 嵌套 token 归因 + spawn 拓扑 + 自洽诊断);
  - **reader Mode B**(`tools/transcript_adapter.py` + `--transcript`,喂 CC transcript 还原观测视图,绕过 hook);
  - **`--scan-projects`** fleet 扫描(扫全 `~/.claude/projects/` → 跨 session 聚合,per-session 错误隔离,scan 永不 exit 2);
  - **dashboard Level ① fleet**(`dashboard/server.py` 薄 stdlib HTTP server 喂 `analyze.py --json` → 静态 HTML/JS 渲染 hero cache + fleet 表 + by-skill + topology + trust 闸;纯 renderer,只读 result JSON);
  - **live 源 + live-tail**(`--source live`/`live:DIR` + 服务端 mtime-poll 自动 refresh + 前端 2s 轮询;C 形态 `analyze.py --watch`);
  - **运行时数据源切换器**(`POST /api/source` 不重启 server 切 scan/live/自定义 + 目录浏览弹层)。
- **🔴 平台硬边界**:CC transcript 只持久化 root 直发结构化 spawn → Mode B 只重建 depth-2,**depth-3+ token / 拓扑须靠 live hook**(Mode A)。
- **dashboard Level ②+ 多级钻取 + hero context 半边**:✅ 已交付(fleet 点行 → 单 session 编排视图 → Level ③ spawn / Level ④ turn;端点 `/api/session·spawn·root·turn/<sid>`;hero 双面板含 root ctx 峰值,逐 turn root context 数据层已建)。
- **真数据基线**:fleet 扫描实测 17 session / 705 spawn / 41.05M token(91.2% cacheRead)/ all consistent / 0 error(跨多个真实 CC 编排项目)。

## 红线(不可违背)

1. **只量不动**——recorder 永不阻断编排。任何异常 swallow + `exit 0`,绝不 `exit 2`。观测失败 = 静默丢这条记录,编排照跑。
2. **零耦合被动**——不改编排、不主动发事件,只挂全局 `PostToolUse` 收割。全工具唯一破此约定处是跨 session 续接的 carrier + lineage(`AGENTINSIGHT_CARRIER_*`,可选,未设则退化,尚未建满),其余全保持零耦合。
3. **别在用来开发本工具的 session 里注册真 hook**——写 `settings.json` hooks 块 / enable plugin = "注册"。**F7**:CC hook 配置 mid-session **不重载**,注册**不会**在当前 session 立即起火(要重启才生效),但**重启后即真 fire**。**⚠️ 若 repo 的 `.claude/settings.local.json` 已挂 hook 配置**,则在该 repo 工作的 session **就不是 inert**——Agent 调用会真 fire、落盘;开发期宜移除该配置(或明知只量不动)。三条底线:① `settings.json` 可能含 auth token、别直接挂 hook(**见红线 5**);② 开发 session 须干净可重复;③ live 验收在隔离 New Session 做。
4. **不回退到旧 token 分析脚本**——本工具不复用任何要被替换掉的旧物。
5. **绝不 cat `~/.claude/settings.json`**——可能含 auth token(红线文件)。查 hook 配置只导出 hooks 键:`python3 -c "import json;print(json.dumps(json.load(open('$HOME/.claude/settings.json')).get('hooks',{})))"`,或看项目级 `.claude/settings.local.json`。
6. **`grep`/`find` 对 `~/.claude/projects` 必须绝对路径 + `--`**——CC project 目录名形如 `-home-user-myproject`(连字符开头)会被当选项吞掉。例:`grep -rn "X" -- "/home/u/.claude/projects/-home-..."`(`--` 终止选项解析)。
7. **重启 dashboard server 用绝对路径**——`dashboard/server.py` 是只读 passive reader(观测只量不动,只喂 `analyze.py --json` 产物);Bash 工具 cwd 在多次调用间持久,相对路径前缀重复拼接会致 python `Exit 2`,故重启一律绝对路径。
8. **单一计费核**——cache 命中率 = `cacheRead/(cacheRead+input+cacheCreation)`(output 永不进缓存);`SkillCall` 零 token 不进 grandTotal / cost 排名;tool 失败绝不并 successRate / grandTotal。

## 三轨边界

- **Agent 轨道**(核心):`PostToolUse(Agent)` → `SubagentCallRecord`。token / 时延 / 成败全在这条,不依赖栈、每事件独立落盘。
- **Skill 轨道**:零 token,只追踪"加载了哪些",不归因 token 成本、不进 grandTotal / cost 排名。
- **Bash 轨道**:opt-in 默认关(高频 × fork-exec 拖慢编排)。无 exit code,结果邻近信号降级为 `interrupted` + `stderr` 文本,非二元过/没过。

## 拓扑(无状态)

- **无 agentStack**——CC 是 fork-exec 短命外部命令,进程内栈物理上不可跨调用累积。
- caller→spawned 靠事件**自带显式字段**:`caller.agentId` = 顶层 `agent_id`(缺失 = root)、`spawned.agentId` = `tool_response.agentId`。
- `parentType` / `callChain` 是 **reader** 从 caller↔spawned 匹配派生的视图,不在线算。
- "orchestrator" 是"caller 缺失 = 根"的角色标签,不是写死的 agent 名(零硬编码,从数据发现)。

## 跨 session 续接(尚未建满)

- `generationId = effective_id`(carrier ? carrier : sessionId)。recorder 已盖,续接就绪;carrier 走 `AGENTINSIGHT_CARRIER_ID`(env)或 `AGENTINSIGHT_CARRIER_FILE`(handoff 文件),二选一、env 优先,未设则退化成 `sessionId`。
- 完整续接(SessionStart hook + lineage log `generations.jsonl` + budgetState 跨 session persist)尚未交付。foreign session(没开本插件 hook)的离线续接须上层在 handoff 时落 lineage(最小契约 `{generationId, sessionId, ts}`),不落则 foreign 档离线缝不上、但不崩。
- 续接机制是纯新设计——无先例兜底,唯一背书 = 已验证原型 + documented CC 原语。

## F 实证发现

> **F 编号** = 实证探查期发现(Finding);本仓带入 6 条立身约定(F3/F5/F6/F7/F8/F9),F1(duration 字段自带)/ F2(usage 命门通过)/ F4(reasoning 计入 output)三条已吸收入实现、不单列。代码注释 / 测试里的 F3/F5/... 即指此处。

- **F3**:Skill 零 token 边界 → `SkillCall` 不入 grandTotal / 不进 cost 排名。
- **F5**:Bash 无 exit code → 降级 `interrupted` + `stderr` 文本启发式。
- **F6**:`resolvedModel` = provider/session 依赖,best-effort(命中即用、缺则 null),不进命门。
- **F7**:CC hook 配置 mid-session 不重载(见红线 3)。
- **F8**:拓扑靠事件自带 caller→spawned 显式链接,不靠时序。
- **F9**:读顶层结构化 `toolUseResult` / 结构化 tool_use blocks OK,**绝不解析 content 伪 XML**。

## 测试纪律

- 新逻辑先写合成 stdin 单测(沿用 `tests/test_record.py` 隔离打法:子进程最小 env + 临时 logDir,不碰真 session)。
- 真平台行为(CC 触发 / env 透传 / 并发)留给 New Session live 验收,不在开发 session 里试。
- per-session / per-file 错误隔离:单点异常 swallow,scan / fleet 永不 exit 2。
