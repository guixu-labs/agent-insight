# Changelog

All notable changes to **agent-insight** are documented here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/);
versions adhere to [Semantic Versioning](https://semver.org/).

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
