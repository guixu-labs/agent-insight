# agent-insight

**English** | [中文](README.md)

**A universal agent / subagent orchestration observability tool.** A passive `PostToolUse` hook records each subagent's **tokens / latency / success-or-failure / skill usage / command results** to a rolling JSONL log; a built-in reader (reads live JSONL or CC's native transcript into a unified Event IR) and a browser dashboard are included. **Opt-in, zero-coupling, measure-only — never interferes.**

An honest ledger for multi-agent orchestration in Claude Code: who spawned whom, how many tokens were spent, which skills were loaded, which command failed. **Deep and precise** — full nesting depth (depth-3+) per-subagent token attribution + spawn call topology + a single accounting core (cache hit rate on the billing basis; zero-token skills don't dilute the totals).

> This README is self-contained (architecture / usage / status). Agent working conventions are in [`AGENTS.md`](AGENTS.md).

## What it solves

When Claude Code runs a large task it dispatches a swarm of subagents, but you can't see:

- How many tokens the whole orchestration burned this run, and which subagent was the most expensive;
- How subagents nest (root → outer → inner), and what the topology looks like;
- What the cache hit rate is (did you save money), and which session has too much fresh input and should be optimized;
- Which session hit the context window, and which spawns took off asynchronously and haven't reported back yet;
- Which skills were loaded, and which Bash / commands failed.

`agent-insight` passively harvests these facts, persists them, and reconstructs them into a queryable ledger. It never changes your orchestration, never emits events on its own, and never blocks (errors are silently dropped for that one record; the orchestration keeps running).

## Current status (v0.1.0)

| Capability | Status |
|---|---|
| **recorder** (three-track hook → rolling JSONL) | ✅ live-fire closed loop (2026-06-16) |
| **reader Mode A** (reads own JSONL, full depth incl. depth-3+) | ✅ delivered |
| **reader Mode B** (reads CC transcript, depth-2, platform limit) | ✅ delivered |
| **`--scan-projects` fleet scan** (scans all of `~/.claude/projects/`) | ✅ delivered |
| **dashboard Level ① fleet** (hero cache + fleet table + by-skill + topology + trust gate) | ✅ delivered |
| **live source + live-tail** (mtime-poll auto-refresh + 2s frontend polling) | ✅ delivered |
| **runtime data-source switcher** (switch scan/live/custom without restarting the server) | ✅ delivered |
| **dark/light theme toggle** (top-right button · remembers choice) | ✅ delivered (dark default · GitHub-Light-sourced palette · localStorage) |
| **`/agent-insight:insight` active entry** (slash command) | ✅ delivered + live-accepted (2026-06-23) |
| Level ② session orchestration view + hero context panel | ✅ delivered (row click → single session → spawn/turn; hero dual-panel incl. root ctx peak) |
| cross-session continuation (SessionStart hook + lineage stitching + budget metering) | ✅ delivered (lineage stitching complete; budgetState cross-session metering delivered, reader-computed, opt-in) |

**Real-data baseline** (measured by fleet scan): **17 sessions / 705 spawns / 41.05M tokens (91.2% cacheRead) / all consistent / 0 errors** — across multiple real CC orchestration projects.

## 🔴 Hard constraint: enabling must happen in a New Session

**Never enable this plugin inside your current dev session.** CC hook config does **not reload mid-session** — "registering" (writing a hooks block to settings.json / enabling the plugin) **will not** fire in the current session (CC reads config once at session start; it takes a restart to take effect). Three hard rules:

- `~/.claude/settings.json` may contain an auth token — **never hang a hook directly off it**; use a project-level `.claude/settings.local.json` instead;
- The current dev session must stay clean and reproducible (registration changes post-restart behavior + needs a restart to take effect = it disrupts development);
- Live acceptance should be done in an isolated New Session for controllable reproduction / rollback.

**Enable flow (do it in a New Session):**

1. Either hand-mount a `PostToolUse` hooks block in a project-level `.claude/settings.local.json` (use the absolute path of `hooks/record.py` for `command`), or `/plugin install` this plugin (see "Installation" below).
2. **Restart CC** (the new process reads the config) → dispatch any subagent for a lightweight acceptance probe (a trivial "pong" probe is enough; no specific runtime needed).
3. Inspect `~/.claude/agent-insight/<project>/<date>.jsonl` for real on-disk records + `tokens` non-null = the critical gate stands live.

> ⚠️ If a repo's `.claude/settings.local.json` already has a hook config mounted, that repo's dev session is **not inert** — Agent calls really do fire and persist. During development, prefer to remove the config, or know that it is measure-only.

## Three capture tracks

| Track | hook | recordType | Default | Value |
|---|---|---|---|---|
| **Agent** | `PostToolUse(Agent)` | `SubagentCall` | always-on | per-subagent tokens / latency / success-or-failure (core) |
| **Skill** | `PostToolUse(Skill)` | `SkillCall` | always-on | which capability skills a subagent loaded (zero tokens, not in grandTotal) |
| **Bash** | `PostToolUse(Bash)` | `Command` | **opt-in, off by default** | `interrupted` + `stderr` of verify / validation commands |

Bash at high frequency × fork-exec would slow the orchestration, so it is off by default and only enabled with `AGENTINSIGHT_BASH=1` — the one v1 feature gated behind opt-in; everything else rides the low-frequency Agent/Skill tracks, always-on and imperceptible.

## Configuration (environment variables)

All prefixed `AGENTINSIGHT_*`. Unset → use the default; zero-config to run.

| env | purpose | default |
|---|---|---|
| `AGENTINSIGHT_LOG_DIR` | JSONL root directory | `~/.claude/agent-insight` |
| `AGENTINSIGHT_PROJECT` | project subdirectory name (grouping) | basename of cwd |
| `AGENTINSIGHT_BASH` | enable the Bash track (`1`/`true`/`yes`) | off |
| `AGENTINSIGHT_BROWSE_ROOT` | trusted root for the dashboard `/api/browse` directory-browser popup | `~` (home) |
| `AGENTINSIGHT_PORT` | dashboard port | `8765` |
| `AGENTINSIGHT_SOURCE` | dashboard initial data source (overridable by `--source`) | `scan` |
| `AGENTINSIGHT_PROJECTS_ROOT` | fleet-scan root directory | `~/.claude/projects` |
| `AGENTINSIGHT_CARRIER_ID` | cross-session continuation carrier (env channel) | none → `generationId` degrades to `sessionId` |
| `AGENTINSIGHT_CARRIER_FILE` | continuation carrier (handoff-file channel) | none |
| `AGENTINSIGHT_BUDGET_THRESHOLD` | token threshold for rolling up cumulative budget across sessions by generationId (reader offline budgetState); **also the real-time emission master switch** (recorder computes+emits only if set) | none → no budget chip, no emission |
| `AGENTINSIGHT_BUDGET_WEBHOOK` | if set, POST a `BudgetEvent` to this URL after every Agent persist (an external handoff tool subscribes); **requires `BUDGET_THRESHOLD` also set** | none → only writes local `budget-events.jsonl` |

### Budget metering (budgetState)

Set `AGENTINSIGHT_BUDGET_THRESHOLD` to a token count (e.g. `60000`); the reader rolls up `grandTotal.total` of all sessions in the same logical workflow (by `generationId`) **across sessions into one sum** (cacheRead-inclusive, same basis as the accounting core) and attaches `result.generations[i].budgetState = {threshold, cumulativeTotal, pctOfThreshold, exceeded}`. The dashboard shows a budget chip in the fleet table's total column + the continuation-group header: <80% green / 80–99% amber / ≥100% (threshold reached = exceeded) red. **No threshold set → the field is absent → the table is byte-for-byte identical to today** (graceful opt-in, no new permanent column). The decision logic has a single source (`tools/budget.py`, shared by reader offline + recorder real-time emission).

**real-time budget emission (per-session, distinct from the cross-session offline above)**: once `AGENTINSIGHT_BUDGET_THRESHOLD` is set, after every Agent persist the recorder additionally computes the **current session** total in real time (matching an external handoff tool's per-session handoff semantics: single session blows the threshold → trigger immediately) and writes `<log_base>/budget-events.jsonl`; if `AGENTINSIGHT_BUDGET_WEBHOOK` is set, it also POSTs. This is for an external handoff tool to subscribe and trigger handoffs in-process — a different purpose from the cross-session budgetState above (offline review). Opt-in (no emission if threshold unset; preserves zero-coupling).

## Output

Rolling JSONL, one file per day, one directory per project:

```
<logDir>/<project>/YYYY-MM-DD.jsonl
```

One record per line, three shapes (`recordType`) — see [`schema/subagent-call.schema.json`](schema/subagent-call.schema.json). The online recorder persists only **raw facts** (`caller` / `spawned` / `tokens`); `parentType` / `callChain` are views derived by the reader from `caller ↔ spawned agentId` matching (stateless).

**Topology attribution**: every event carries an explicit caller→spawned link — `caller.agentId` = top-level `agent_id` (absent = root direct dispatch), `spawned.agentId` = `tool_response.agentId`. Accurate even with concurrent waves (each record names its caller; it doesn't rely on timing).

## Installation

Two paths, pick by scenario:

**1. Claude Code plugin (main path)** — `/plugin marketplace add guixu-labs/agent-insight` → `/plugin install agent-insight`. Restart CC to take effect. Hooks are wired automatically.

**2. Zero-dependency clone (stdlib only)** — after `git clone`, run `python3 dashboard/server.py` or `python3 tools/analyze.py` directly. Pure standard library, no pip install.

## reader (`tools/analyze.py` · Mode A + Mode B)

Reads the persisted JSONL / CC transcript back, reconstructs the token ledger + call topology + self-consistency diagnostics — no rerun, zero-coupling.

### Which to pick: Mode A or Mode B?

**One-line rule**: that session **ever had this plugin's hook mounted** (record.py persisted anything) → Mode A (full depth, incl. depth-3+ nested tokens); **never mounted / someone else's / an old session from before the plugin** → only Mode B (depth-2; the CC transcript doesn't persist nested structured spawns — a platform limit). Both produce **the same Event IR / the same output format**; the only difference is nesting depth.

| The data you have | Where | Mode | Depth |
|---|---|---|---|
| JSONL dropped by this plugin's hook | `~/.claude/agent-insight/<project>/<date>.jsonl` | **A** | full (incl. depth-3+ real nesting) |
| CC native transcript (every session has one) | `~/.claude/projects/<project>/<sid>.jsonl` | **B** | depth-2 only |

```bash
# Scan all projects under the default logdir (most common)
python3 tools/analyze.py

# Only one project / from a certain date / a single file
python3 tools/analyze.py --project my-project
python3 tools/analyze.py --since 2026-06-16
python3 tools/analyze.py --jsonl ~/.claude/agent-insight/my-project/2026-06-16.jsonl

# Per-call chain (depth / parentType / orphan markers)
python3 tools/analyze.py --tree

# Machine-readable (downstream / dashboard / CI)
python3 tools/analyze.py --json

# C-form live-tail (CLI): 2s poll · clear+reprint only when mtime changes · Ctrl-C to exit
python3 tools/analyze.py --watch
```

Three output blocks:

- **Token ledger**: grandTotal (input/output/cacheCreation/cacheRead/total) + aggregation by `subagentType` (calls/total/avgDur/successRate, sorted by total desc);
- **Call topology**: call graph (`parentType → childType` edges × trigger count); `--tree` additionally gives a per-call `callChain` (grouped by sessionId, offline `agent_id` linking, `orchestrator` role label prefixed at the root);
- **Self-consistency diagnostics**: `isRoot` invariant cross-check, orphan caller (caller not captured in this session → the nested inner wasn't recorded, not a consistency violation), null spawned / null tokens annotations.

### Mode B — feed a CC transcript (bypass the hook)

For sessions that never had this plugin's hook on (someone else ran / an old session), as long as the original CC transcript is around, you can still offline-reconstruct per-subagent tokens + topology — no hook, no rerun.

```bash
# Feed one root-session transcript (auto-discovers sibling <sid>/subagents/agent-*.jsonl)
python3 tools/analyze.py --transcript ~/.claude/projects/<proj>/<sid>.jsonl --json

# Feed a whole session directory (root .jsonl + subagents/)
python3 tools/analyze.py --transcript ~/.claude/projects/<proj>/<sid>/ --tree
```

Reuses the same Mode A pipeline, only swapping the ingest entry point (`tools/transcript_adapter.py` parses the transcript's `toolUseResult`). Output format is identical to Mode A.

**🔴 Hard platform limit**: the CC transcript **only persists structured `toolUseResult` for root-direct Agent calls**; nested calls (a child dispatching another child) only land as message-content text blocks, that row's `toolUseResult=null` → **Mode B can only rebuild depth-2 (root→agent); depth-3+ tokens / topology require the live hook**. Three independent real samples verified all-depth-2, zero nesting. **2026-06-24 live follow-up**: the `general-purpose` subagent doesn't recurse via the Agent tool by default (CC orchestrations basically cap at depth-2); the depth-3+ attribution capability is retained (synthetic unit-tested), but live real data is overwhelmingly depth-2 — the robust selling point is **depth-2 (mainstream) precise per-subagent attribution + topology + single accounting core**.

### Mode B · fleet — scan a whole projects directory (`--scan-projects`)

One-shot an aggregated **fleet report** across all historical orchestration sessions: per-session summary + cross-session totals + top outliers + scan-level self-consistency + depth-2 banner. Fully offline, hook-free — zero impact on the current session.

```bash
# Bare flag → scan the full fleet (~/.claude/projects)
python3 tools/analyze.py --scan-projects

# Scan only one project subdirectory
python3 tools/analyze.py --scan-projects ~/.claude/projects/-home-user-myproject

# Machine-readable
python3 tools/analyze.py --scan-projects --json
```

Runs the same Mode B pipeline per session (**per-session error isolation**: a single session's exception goes into `errors[]`, scan never `exit 2`, the rest scan normally), then merges across sessions. **Merge correctness**: `avgDurationMs`/`successRate` are rates — **recomputed from raw accumulators (`durSum`/`durN`/`successCount`), never averaging averages**.

## dashboard (browser · Level ① fleet)

A thin stdlib HTTP server feeds the `analyze.py --json` output; static HTML/JS renders the fleet overview (hero cache panel + fleet table + by-skill slice + topology + trust gate). Zero external dependencies, assets vendored, no CDN.

**Dark/light theme**: toggle via the ☀️ / 🌙 button at the top-right. **Dark by default**; once you switch to light (GitHub-Light-sourced palette) the choice is stored in the browser's `localStorage` and remembered on refresh — it does not follow the system. Clear `localStorage`'s `ai-theme` to restore the default dark.

```bash
# Default scan source (scans ~/.claude/projects)
python3 dashboard/server.py
# Open http://127.0.0.1:${AGENTINSIGHT_PORT:-8765} in a browser

# Specify a data source / port
python3 dashboard/server.py --source scan:~/.claude/projects/-home-user-myproject --port 9000
AGENTINSIGHT_PORT=9000 python3 dashboard/server.py --source transcript:~/.claude/projects/<proj>/<sid>.jsonl

# Pin the initial source via env (deploy / systemd / container)
AGENTINSIGHT_SOURCE=live python3 dashboard/server.py
```

Data sources (`--source` / `AGENTINSIGHT_SOURCE`): `scan` (default) / `scan:DIR` / `transcript:PATH` / `jsonl:PATH` / `file:PATH` (read a result-JSON snapshot directly) / `live` (live logdir) / `live:DIR`. **Bare-path auto-detection**: type a directory or `.jsonl` path straight into the path box (no prefix) and the server infers it (directory → `scan:`, or `live:` if under the live-logdir base; `.jsonl` → `transcript:`; otherwise → 400).

### Three orthogonal axes (don't conflate)

- **Source axis** — where the data comes from: live (record.py hook real-time orchestration log, full nesting depth) / scan·transcript (CC native transcript, depth-2). Shown in the "data source" dropdown, **not in the chip**.
- **Refresh axis** — how often the page re-pulls: live-tail on (2s frontend poll + server-side mtime-poll recompute) / off (frozen).
- **Liveness axis** — whether the data is moving: the file's most-recent update within ≤ 300s counts as moving.

**mode chip three states = refresh axis × liveness axis**: live-tail off → `⏸ paused`; on + moving → `● live`; on but long-still (old session) → `⏳ idle` (the next poll after the file resumes updating flips back to `● live`). The chip is orthogonal to source: scan source + live-tail on + a new session dropping → the chip shows `● live` (not "offline").

### Page-element cheat sheet

- **① banner** `✓ 0 anomalies · N sessions · N spawns` — counts each of three signal classes: 💥 context blown, ⚠ low hit rate (<60%), ⏳ async not-reported-back. All 0 → green; any >0 → split into red segments.
- **② health column** (the last column's icon in the fleet table) — each session row shows one icon for its most severe signal: 💥 > ⚠ > ⏳ > ✓. Also the sort key, so problem sessions sink to the top automatically.
- **③ mode chip** — see above (refresh × liveness three states).
- **④ ✗ tool failures** — deliberately not on the fleet, only on detail pages. Bash non-zero exits, Edit misses, etc. (`is_error`) sink down to the root/spawn/turn row where the problem occurred.

## `/agent-insight:insight` (active query entry)

After installing the plugin, CC auto-discovers it as a slash command per the `commands/<name>.md` convention. **CC forces a `<plugin-name>:` namespace prefix**, so the actual registered name is **`/agent-insight:insight`** (not `/insight`). 2026-06-23 live acceptance confirmed it runs: command name + `${CLAUDE_PLUGIN_ROOT}` variable substitution + Mode A data read. See [commands/insight.md](commands/insight.md).

- `/agent-insight:insight` — current-session orchestration summary in-chat + dashboard localhost URL;
- `/agent-insight:insight live` — switch to live-source tail;
- `/agent-insight:insight session <id>` — drill into a single session;
- `/agent-insight:insight scan` — run one fleet-scan summary.

The plugin is **passive** by default (hooks only); `/agent-insight:insight` adds the active query entry.

## Tests

```
python3 tests/test_record.py              # recorder persistence logic (73/73)
python3 tests/test_analyze.py             # Mode A reader topology + self-consistency + live-tail --watch (144/144)
python3 tests/test_transcript_adapter.py  # Mode B transcript ingest (83/83)
python3 tests/test_scan_projects.py       # Mode B fleet scan + cross-session merge (102/102)
python3 tests/test_dashboard.py           # dashboard server contract + scaffolding + live source/switcher/browser popup (636/636)
```

All five are isolated (subprocess + temp dir / env), **never touching real sessions / settings.json**. **All green** (2026-06-23 cleared the historical token-basis debt: `grandTotal.total` unified to a four-bucket sum including cacheRead, fixtures aligned).

## Live self-check (whole hook → record → reader verification)

Synthetic unit tests can't cover **real CC platform behavior** — whether the hook actually fires, tokens are actually passed through, depth-3+ nesting is actually captured.

**Prerequisite**: first install the plugin per "Installation" above (`/plugin marketplace add guixu-labs/agent-insight` → `/plugin install agent-insight`, or hand-mount a `PostToolUse` hook in a project-level `.claude/settings.local.json` with `command` pointing at the absolute path of `hooks/record.py`), and run it in an **isolated New Session** (red line 3 / F7: hook config doesn't reload mid-session — restart CC to fire). If the hook isn't firing, `live_check` reports `NO_DATA`.

⚠️ **Important**: `live_check` **only reads already-persisted data — it doesn't produce any itself**. Running it bare yields `NO_DATA` (no orchestration activity in this session yet). You must **first** use `--show-probe` to get a prompt, feed it to CC so CC dispatches a subagent and the hook persists a record, **then** run the check:

```bash
# Step 1 (produce data, must do first): get a trigger prompt → paste to CC → CC dispatches a subagent → hook persists
python3 tools/live_check.py --show-probe agent      # Agent-track basic capture (dispatch general-purpose, replies pong)
python3 tools/live_check.py --show-probe nested     # (optional) depth-3+ nesting, verifies the core moat

# Step 2 (verify): after CC finishes, read on-disk JSONL + full assertions + verdict
python3 tools/live_check.py
```

Verdict: `LIVE_OK` (core chain stands: Agent track persisted + token gate + self-consistent) / `CORE_FAIL` (gate failed — take the result back to debug `hooks/record.py` field paths) / `NO_DATA` (hook not mounted). The depth-3+ nested-capture (core moat) is reported separately — captured with correct nested-layer caller = moat holds; otherwise it reports honestly (LLM didn't spawn enough layers / a platform limit was found). It's a status reporter, never blocks orchestration (`exit 0`; only a real error `exit 1`). See `python3 tools/live_check.py --help`.

## Form and boundaries

- **A standalone plugin** (not a skill / agent); the core is `hooks/`. No `agents/`, no `workflows/`.
- **Zero-coupling passive observation**: it never changes the orchestration, never emits events on its own — it only hangs off the global `PostToolUse` to harvest the event stream. The one place "zero-coupling" is broken is the cross-session continuation carrier + lineage convention (optional; unset → degrades).
- **Measure-only**: the recorder never blocks the orchestration (errors swallowed + `exit 0`).
- **v1 platform scope**: locked to the Claude Code target.

## License

MIT.
