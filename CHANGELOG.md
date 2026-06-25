# Changelog

All notable changes to **agent-insight** are documented here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/);
versions adhere to [Semantic Versioning](https://semver.org/).

## [0.1.1] — 2026-06-24

Incremental release: bilingual docs, a live self-check tool, and the first
active-event seam (budget emission for a handoff action layer).

### Docs

- **Bilingual README** — `README.md` (Chinese) + `README.en.md` (English)
  with a top language toggle.
- **depth-3+ claims calibrated** — live testing confirmed CC subagents don't
  recurse via the Agent tool by default, so depth-3+ nesting is rare in
  practice. Selling point refocused on depth-2 precise per-subagent
  attribution + topology + single accounting core. The attribution logic
  still supports any depth (synthetic unit-tested).

### Tools

- **`tools/live_check.py`** — whole `hook → record → reader` live self-check.
  `--show-probe agent/nested/skill/bash` prints trigger prompts; a bare run
  reads on-disk JSONL, runs full assertions (Agent track + token gate +
  depth-3+ + Skill/Bash + lineage + consistency), and reports a verdict
  (`LIVE_OK` / `CORE_FAIL` / `NO_DATA`). Status reporter, never blocks.

### Real-time budget emission (opt-in · per-session) (opt-in · per-session)

- New `tools/budget.py` — single source for `_budget_threshold` / `_budget_state`
  (moved out of analyze.py) + `_session_cumulative` (per-session real-time total).
- `hooks/record.py` emits a `BudgetEvent` after each Agent persist when
  `AGENTINSIGHT_BUDGET_THRESHOLD` is set: writes `budget-events.jsonl`
  (default, for an external handoff tool to tail) and POSTs to `AGENTINSIGHT_BUDGET_WEBHOOK`
  if set. Per-session cumulative (matches an external handoff tool's per-session handoff
  semantics); opt-in (no threshold → inert, preserves zero-coupling);
  failures swallowed (never blocks).
- New env: `AGENTINSIGHT_BUDGET_WEBHOOK`. This is the tool's **first
  active-event seam** — a controlled break of "measure-only, passive",
  documented alongside carrier/lineage.

## [0.1.0] — 2026-06-23

First public release. Passive observability for Claude Code agent/subagent
orchestration — **opt-in, zero-coupling, measure-only**.

### Recorder

- Three-track `PostToolUse` hook → rolling JSONL, **never blocks orchestration**
  (every failure is swallowed + `exit 0`).
- **Agent** track (always-on) — per-subagent token / latency / success-failure.
- **Skill** track (always-on) — which subagent loaded which skills (zero token,
  never billed).
- **Bash** track (opt-in, `AGENTINSIGHT_BASH=1`) — `interrupted` + `stderr`.

### Reader

- **Mode A** (`--source live` / `--project`) — reads the recorder's own JSONL;
  **full nested depth** (depth-3+) per-subagent token attribution + spawn
  call topology + self-consistent diagnostics.
- **Mode B** (`--transcript` / `--scan-projects`) — ingests a Claude Code
  transcript; depth-2 (platform boundary: CC only persists depth-2 structured
  spawns, so deeper nesting requires the live hook).
- **Fleet scan** — `--scan-projects` sweeps `~/.claude/projects/` with
  per-session error isolation (scan never `exit 2`).

### Dashboard

- stdlib HTTP server (no framework) feeding static HTML/JS —
  `python3 dashboard/server.py`.
- Multi-level drill: fleet table → single-session orchestration →
  spawn detail → turn transcript.
- Hero dual-panel: cache-hit channel + root-context peak channel;
  by-skill, topology, and a trust gate.
- **Live tail** (`--source live`) + runtime data-source switcher
  (`POST /api/source`) — switch scan/live/custom directory without a restart.

### Cross-session continuation

- Carrier (`AGENTINSIGHT_CARRIER_ID` env or `AGENTINSIGHT_CARRIER_FILE`
  handoff file) lets multiple Claude Code sessions of one logical workflow
  share a `generationId`; a `SessionStart` hook writes a global lineage map
  and the reader stitches sessions together. Without a carrier it degrades
  cleanly to `generationId = sessionId` (today's behavior).

### Slash command

- `/agent-insight:insight` — in-chat orchestration summary + dashboard URL,
  with `live` / `session <id>` / `scan` subcommands.

### Billing core

`grandTotal.total = input + output + cacheCreation + cacheRead`
(**cacheRead counts**; the provider's own `totalTokens` is never read).
Cache hit rate =
`cacheRead / (cacheRead + input + cacheCreation)` (output never cached).
Skill calls are zero-token and never enter the grand total or cost ranking;
tool failures never count toward success rate or the grand total.

### Install

- Claude Code plugin marketplace:
  `/plugin marketplace add guixu-labs/agent-insight`
  → `/plugin install agent-insight`.
- Zero-dependency clone (Python stdlib only):
  `python3 dashboard/server.py`.
