"""agent-insight dashboard A 形态 server 测试 (子进程 + HTTP, 隔离).

范式同 test_scan_projects.py: 子进程起 dashboard/server.py (file: fixture 数据源),
urllib GET 断言契约。不碰真 session / settings.json / marketplace.json。
"""
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "..", "dashboard", "server.py")
PLUGIN = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
try:
    from transcript_adapter import agent_turn_traces, agent_spawn_head, agent_turn_raw
except Exception:
    agent_turn_traces = agent_spawn_head = agent_turn_raw = None
PASSED = 0


def check(cond, label):
    global PASSED
    assert cond, f"FAIL: {label}"
    PASSED += 1
    print(f"  ok: {label}")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _start(port, source):
    return subprocess.Popen(
        [sys.executable, SERVER, "--port", str(port), "--source", source],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=PLUGIN)


def _wait_ready(port, timeout=10):
    """轮询 /api/result 直到 200 (file 源同步就绪) 或超时."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/result", timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.2)
    return False


def _get(port, path):
    url = f"http://127.0.0.1:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        # urlopen 对 4xx/5xx 直接抛 HTTPError; 提取 code+body 以便断言 404 等 (D7 依赖).
        return e.code, e.read().decode("utf-8", errors="replace")


def _post(port, path, body):
    """POST JSON body → (status, body_str). 4xx/5xx 经 HTTPError 捕获 (D14 断言 400 依赖)."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


def _fixture():
    return {
        "mode": "B · scan-projects", "modeLabel": "B · scan-projects",
        "sessionsScanned": 2, "sessionsSkipped": 0, "spawnsTotal": 5, "scanDir": "/tmp/x", "project": None,
        "errors": [],
        "grandTotal": {"input": 100, "output": 10, "cacheCreation": 20, "cacheRead": 70, "total": 200},
        "bySubagentType": [], "bySkill": [], "callGraph": [],
        "perSession": [{"project": "p", "sid": "deadbeef-1234-5678-9abc-def012345678", "spawns": 5,
                        "totalTokens": 200, "cacheReadPct": 35.0, "durationS": 120, "consistent": True,
                        "modeLabel": "B · transcript",
                        "grandTotal": {"input": 100, "output": 10, "cacheCreation": 20, "cacheRead": 70, "total": 200}}],
        "topSessions": [], "scanConsistency": {"allConsistent": True, "violatingSessions": []},
        "depth2Note": "Mode B 恒 depth-2 (§9.3#1).",
    }


# element-id 契约: server/index.html/app.js/test 四方共识 (Task 5 app.js 按此填)
_SCAFFOLD_IDS = ["meta", "trust-banner", "hero-cache-body", "hero-context-body",
                 "fleet-table", "skill-table"]
_CSS_TOKENS = ["#0b0e13", "#58a6ff", "#3fb950", "#f0883e", "#f85149"]  # §8.9 dark 色板


def test_api_result_file_source():
    """D1: 起 server (file: fixture) → GET /api/result == fixture."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(_fixture(), f)
        fxpath = f.name
    port = _free_port()
    proc = _start(port, f"file:{fxpath}")
    try:
        check(_wait_ready(port), "D1 server ready (file source)")
        status, body = _get(port, "/api/result")
        check(status == 200, "D1 /api/result 200")
        got = json.loads(body)
        check(got["grandTotal"]["total"] == 200, "D1 grandTotal.total == 200")
        check(got["sessionsScanned"] == 2, "D1 sessionsScanned == 2")
        check(got["perSession"][0]["grandTotal"]["input"] == 100, "D1 perSession[0].grandTotal.input == 100")
    finally:
        proc.terminate()
        proc.wait()
        os.unlink(fxpath)


def test_static_routes_and_scaffolding():
    """D2: GET / → HTML 含 scaffolding id; D3: GET /static/* → 资产 + CSS 色板 token."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(_fixture(), f); fxpath = f.name
    port = _free_port()
    proc = _start(port, f"file:{fxpath}")
    try:
        assert _wait_ready(port)
        # D2: index.html scaffolding
        status, html = _get(port, "/")
        check(status == 200, "D2 GET / 200")
        for eid in _SCAFFOLD_IDS:
            check(f'id="{eid}"' in html, f"D2 html 含 #{eid}")
        # D3: 静态资产路由 (style.css) + CSS 色板. (app.js 在 Task 5 才建 → 不在此测;
        #     /static/ 路由由 style.css 此处证, app.js 复用同一路径, Task 5 浏览器 smoke 加载它)
        s_css, css = _get(port, "/static/style.css")
        check(s_css == 200, "D3 GET /static/style.css 200")
        for tok in _CSS_TOKENS:
            check(tok in css, f"D3 css 含色板 {tok}")
    finally:
        proc.terminate(); proc.wait(); os.unlink(fxpath)


def test_scan_source_and_refresh():
    """D4: scan 源 shell analyze.py --scan-projects → fleet 顶层 key + 非空解析 (全链路);
       D5: file 源 /api/refresh 热更新 (改文件后 refresh 见新值)."""
    # --- D4: scan 源. toolUseResult 形状复用 test_scan_projects.agent_line (已验证可解析为 depth-2) ---
    tmp = tempfile.mkdtemp()
    proj = os.path.join(tmp, "-home-fakeproj")
    sid = "11112222-3333-4444-5555-666677778888"  # UUID 形 (discover_root_transcripts 过滤规则)
    os.makedirs(proj, exist_ok=True)
    agent_jsonl = json.dumps({
        "timestamp": "2026-06-17T10:00:00+08:00", "sessionId": "s-x", "isSidechain": False,
        "type": "assistant", "uuid": "u-agent-fake-1", "message": {"role": "assistant"},
        "toolUseResult": {"status": "completed", "agentId": "agent-fake-1", "agentType": "Explore",
                          "totalDurationMs": 5000,
                          "usage": {"input_tokens": 1000, "output_tokens": 500,
                                    "cache_creation_input_tokens": 0, "cache_read_input_tokens": 8500},
                          "totalTokens": 10000}}, ensure_ascii=False)
    with open(os.path.join(proj, sid + ".jsonl"), "w") as f:
        f.write(agent_jsonl + "\n")
    port = _free_port()
    proc = _start(port, f"scan:{tmp}")
    try:
        check(_wait_ready(port), "D4 server ready (scan source)")
        status, body = _get(port, "/api/result")
        check(status == 200, "D4 scan source /api/result 200")
        got = json.loads(body)
        check(got.get("mode") == "B · scan-projects", "D4 scan result mode 标识")
        check("callGraph" in got and "bySubagentType" in got and "perSession" in got,
              "D4 scan result 含 fleet 顶层 key")
        check(len(got.get("perSession", [])) >= 1, "D4 scan 解析出 ≥1 session (非空 · 全链路)")
    finally:
        proc.terminate(); proc.wait()

    # --- D5: file 源热更新 (另起一个 file-source server, 改文件后 /api/refresh 见新值) ---
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(_fixture(), f); fxpath = f.name
    port2 = _free_port()
    proc2 = _start(port2, f"file:{fxpath}")
    try:
        assert _wait_ready(port2)
        _, body1 = _get(port2, "/api/result")
        check(json.loads(body1)["grandTotal"]["total"] == 200, "D5 初始 total == 200")
        # 改文件
        fx = _fixture(); fx["grandTotal"]["total"] = 999
        with open(fxpath, "w") as f: json.dump(fx, f)
        s_ref, _body2 = _get(port2, "/api/refresh")
        check(s_ref == 200, "D5 /api/refresh 200")
        _, body3 = _get(port2, "/api/result")
        check(json.loads(body3)["grandTotal"]["total"] == 999, "D5 refresh 后 total == 999 (热更新)")
    finally:
        proc2.terminate(); proc2.wait(); os.unlink(fxpath)


def test_live_source():
    """D10: live 源 (record.py JSONL · Mode A) → /api/result 含 perSession + modeLabel 'A · live'.
    §9 双数据源: live perSession 字段集须与 offline (Mode B) 一致 → dashboard 同形渲染."""
    tmp = tempfile.mkdtemp()
    proj = os.path.join(tmp, "demo-live")
    os.makedirs(proj, exist_ok=True)
    rec = json.dumps({
        "schemaVersion": 1, "timestamp": "2026-06-18T08:12:40+08:00",
        "runId": "live-sid-1", "projectName": "demo-live", "sessionId": "live-sid-1",
        "toolUseId": "call-live-1",
        "caller": {"agentId": None, "agentType": None, "isRoot": True},
        "recordType": "SubagentCall", "subagentType": "general-purpose",
        "spawned": {"agentId": "a-live-1", "agentType": "general-purpose"},
        "tokens": {"input": 100, "output": 50, "cacheCreation": 0, "cacheRead": 200, "total": 350},
        "durationMs": 5000, "resolvedModel": "glm-5.1", "success": True, "error": None,
    }, ensure_ascii=False)
    with open(os.path.join(proj, "2026-06-18.jsonl"), "w") as f:
        f.write(rec + "\n")
    port = _free_port()
    proc = _start(port, f"live:{tmp}")
    try:
        check(_wait_ready(port), "D10 server ready (live source)")
        status, body = _get(port, "/api/result")
        check(status == 200, "D10 live source /api/result 200")
        got = json.loads(body)
        ps = got.get("perSession", [])
        check(len(ps) >= 1, "D10 live 解析出 ≥1 session perSession (Mode A 吐 perSession)")
        if ps:
            r0 = ps[0]
            check(r0.get("modeLabel") == "A · live", "D10 perSession modeLabel == 'A · live'")
            check(r0.get("totalTokens") == 350, "D10 live perSession totalTokens == 350")
            check(r0.get("project") == "demo-live", "D10 live perSession project == demo-live")
            # 字段集与 offline (Mode B perSession) 逐字段一致 → dashboard 同形渲染 (§9 双数据源)
            # Phase 3: +generationId (跨 session 续接 effective_id; == sid 则无 carrier). 契约 14→15.
            expect = {"project", "sid", "generationId", "spawns", "totalTokens", "cacheReadPct",
                      "durationS", "consistent", "modeLabel", "grandTotal", "ctxPeak",
                      "ctxLimitErrors", "rootUsage", "asyncCount", "toolErrorCount"}
            check(set(r0.keys()) == expect, "D10 live perSession 字段集 == Mode B 契约 (双数据源同形)")
    finally:
        proc.terminate(); proc.wait()


def test_live_tail_mtime_poll():
    """D11: live-tail mtime-poll — 追加 record 后 /api/result 自动 refresh (非手刷 /api/refresh).
    §8.8: server watch source 读的文件 mtime; 变化 → /api_result 内联 _refresh → 前端轮询即见增量."""
    tmp = tempfile.mkdtemp()
    proj = os.path.join(tmp, "demo-tail")
    os.makedirs(proj, exist_ok=True)
    fp = os.path.join(proj, "2026-06-18.jsonl")

    def _rec(tid, tot):
        return json.dumps({
            "schemaVersion": 1, "timestamp": "2026-06-18T08:12:40+08:00",
            "runId": "tail-sid", "projectName": "demo-tail", "sessionId": "tail-sid",
            "toolUseId": tid,
            "caller": {"agentId": None, "agentType": None, "isRoot": True},
            "recordType": "SubagentCall", "subagentType": "general-purpose",
            "spawned": {"agentId": "a-" + tid, "agentType": "general-purpose"},
            "tokens": {"input": 100, "output": 50, "cacheCreation": 0, "cacheRead": 0, "total": tot},
            "durationMs": 1000, "resolvedModel": "glm-5.1", "success": True, "error": None,
        }, ensure_ascii=False)

    with open(fp, "w") as f:
        f.write(_rec("call-1", 300) + "\n")
    port = _free_port()
    proc = _start(port, f"live:{tmp}")
    try:
        check(_wait_ready(port), "D11 server ready (live-tail)")
        _, body1 = _get(port, "/api/result")
        t1 = json.loads(body1).get("grandTotal", {}).get("total", 0)
        check(t1 == 300, "D11 初始 grandTotal == 300")
        time.sleep(0.1)                       # mtime 粒度保险 (ext4 ns-precision 足够)
        with open(fp, "a") as f:              # 追加第 2 条 (模拟 live hook 落盘)
            f.write(_rec("call-2", 500) + "\n")
        # 不调 /api/refresh; 直接 GET /api/result → mtime-poll 应自动 refresh
        _, body2 = _get(port, "/api/result")
        g2 = json.loads(body2)
        t2 = g2.get("grandTotal", {}).get("total", 0)
        check(t2 == 800, "D11 追加后 grandTotal == 800 (mtime-poll 自动 refresh, 非手刷)")
        ps = g2.get("perSession", [])
        if ps:
            check(ps[0].get("spawns") == 2, "D11 perSession spawns == 2 (追加增量可见)")
    finally:
        proc.terminate(); proc.wait()


def test_live_tail_frontend_contract():
    """D12: 前端 live-tail 契约 (§8.8 实时层). JS 行为难单测 → 测契约锚点:
    index.html 含 #live-toggle 按钮; app.js 含 initLiveTail 定义+调用 + setInterval(轮询)
    + #fleet-view hidden 守卫 (drill 时跳过重渲) + document.hidden (tab 切走暂停)."""
    html = open(os.path.join(HERE, "..", "dashboard", "static", "index.html")).read()
    appjs = open(os.path.join(HERE, "..", "dashboard", "static", "app.js")).read()
    check('id="live-toggle"' in html, "D12 index.html 含 #live-toggle 按钮")
    check(appjs.count("initLiveTail") >= 2, "D12 app.js 定义 + 调用 initLiveTail (def + call)")
    check("setInterval" in appjs, "D12 app.js 含 setInterval (2s 轮询)")
    check("document.hidden" in appjs, "D12 app.js 含 document.hidden 守卫 (tab 切走暂停)")
    check('fleet-view' in appjs, "D12 app.js 引用 #fleet-view (drill 时跳过重渲守卫)")
    check("visibilitychange" in appjs, "D12 app.js 含 visibilitychange (tab 回前台恢复轮询)")


def test_session_drill():
    """D7: scan 源 server → GET /api/session/<sid> 返 callChains (per-spawn) + rootContext (逐 turn 曲线).
    fixture root transcript 同时含 assistant usage 行 (→ rootContext) 和 Agent toolUseResult 行 (→ callChains)."""
    import json as _json
    tmp = tempfile.mkdtemp()
    proj = os.path.join(tmp, "-home-fakeproj")
    sid = "11112222-3333-4444-5555-666677778888"  # UUID 形
    os.makedirs(proj, exist_ok=True)
    root_path = os.path.join(proj, sid + ".jsonl")
    lines = [
        # assistant usage turn → rootContext.samples (ctx 3000+6000=9000)
        _json.dumps({"type": "assistant", "timestamp": "2026-06-17T10:00:00+08:00",
                    "message": {"role": "assistant",
                                "usage": {"input_tokens": 3000, "cache_creation_input_tokens": 0,
                                          "cache_read_input_tokens": 6000, "output_tokens": 100}}}),
        # Agent spawn 行 → callChains (1 spawn)
        _json.dumps({"timestamp": "2026-06-17T10:01:00+08:00", "sessionId": sid, "isSidechain": False,
                    "type": "assistant", "uuid": "u-1", "message": {"role": "assistant"},
                    "toolUseResult": {"status": "completed", "agentId": "agent-1", "agentType": "Explore",
                                      "totalDurationMs": 5000,
                                      "usage": {"input_tokens": 500, "output_tokens": 50,
                                                "cache_creation_input_tokens": 0, "cache_read_input_tokens": 4500},
                                      "totalTokens": 5050}}),
    ]
    with open(root_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    port = _free_port()
    proc = _start(port, f"scan:{tmp}")
    try:
        check(_wait_ready(port), "D7 server ready (scan source)")
        status, body = _get(port, f"/api/session/{sid}")
        check(status == 200, "D7 /api/session/<sid> 200")
        got = _json.loads(body)
        check(len(got.get("callChains", [])) >= 1, "D7 callChains 非空 (≥1 spawn)")
        rc = got.get("rootContext") or {}
        check(len(rc.get("samples", [])) >= 1, "D7 rootContext.samples 非空 (逐 turn 曲线)")
        check(rc.get("peak") == 9000, f"D7 rootContext.peak == 9000, got {rc.get('peak')}")
        check(rc.get("limit") == 200000, "D7 rootContext.limit 透传")
        # 不存在的 sid → 404
        s404, _ = _get(port, "/api/session/nope-nope-nope")
        check(s404 == 404, "D7 不存在 sid → 404")
    finally:
        proc.terminate(); proc.wait()


def test_appjs_id_consistency():
    """D6: app.js 引用的 element id 全部可渲染 (静态 index.html scaffolding 或 app.js 动态模板) — 防 id 漂移."""
    # 直接读源文件 (不经 HTTP, 纯静态一致性检查)
    appjs = open(os.path.join(HERE, "..", "dashboard", "static", "app.js")).read()
    html = open(os.path.join(HERE, "..", "dashboard", "static", "index.html")).read()
    # app.js 里 getElementById('xxx') / querySelector('#xxx …') 的 id.
    # querySelector 正则放宽为捕获 '#id' 前缀 (app.js 用 '#fleet-table tbody' 带空格, 需容忍后续内容).
    ids_used = set(re.findall(r"getElementById\(['\"]([\w-]+)['\"]\)", appjs)) \
             | set(re.findall(r"querySelector\(['\"]#([\w-]+)", appjs))
    check(len(ids_used) > 0, "D6 app.js 至少引用 1 个 element id")
    # id 须可渲染: 静态 scaffolding (index.html) 或 app.js 动态模板 (showSession 等运行时 innerHTML 段,
    # 如 #agents-panel 由 app.js 渲染, 非静态 scaffolding). 两处之一命中即满足 "DOM 中存在" 意图.
    for eid in ids_used:
        check(f'id="{eid}"' in html or f'id="{eid}"' in appjs,
              f"D6 app.js 的 #{eid} 在 index.html 或 app.js 动态模板可渲染")
    for must in ["fleet-table", "skill-table", "hero-cache-body"]:
        check(must in ids_used, f"D6 app.js 渲染锚点 #{must} 被使用")


def test_agent_turn_traces():
    """Plan C T1: agent_turn_traces 逐 turn summary (content-safe).
    fixture: 一个 agent-*.jsonl, 含 1) 非零 usage assistant turn (real) +
    2) 零 usage assistant turn (带 tool_use, 配对一个 tool_result → 字符数代理) +
    3) tool name+target 抽取 + outlier (burden>1.5×mean)."""
    assert agent_turn_traces is not None, "transcript_adapter.agent_turn_traces 可用"
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, "agent-t1.jsonl")
    lines = [
        # turn 0: 非零 usage → real token. cacheRead=9999 故意大, 验证 burden 剔 cacheRead (input+cc=6000, 非 15999)
        json.dumps({"type": "assistant", "timestamp": "2026-06-18T10:00:00+08:00",
                    "message": {"role": "assistant", "stop_reason": "end_turn",
                                "usage": {"input_tokens": 1000, "cache_read_input_tokens": 9999,
                                          "cache_creation_input_tokens": 5000, "output_tokens": 50},
                                "content": [{"type": "text", "text": "thinking"}]}}),
        # turn 1: 零 usage + tool_use(Read) → proxy, 配对 tool_result 6000 字符
        json.dumps({"type": "assistant", "timestamp": "2026-06-18T10:00:05+08:00",
                    "message": {"role": "assistant", "stop_reason": "tool_use",
                                "usage": {"input_tokens": 0, "cache_read_input_tokens": 0,
                                          "cache_creation_input_tokens": 0, "output_tokens": 0},
                                "content": [{"type": "tool_use", "id": "tu-1",
                                             "name": "Read", "input": {"file_path": "src/big.py"}}]}}),
        # 配对 tool_result (user turn, content 为 str)
        json.dumps({"type": "user", "timestamp": "2026-06-18T10:00:06+08:00",
                    "message": {"role": "user", "content": [{"type": "tool_result",
                                "tool_use_id": "tu-1", "content": "x" * 6000}]}}),
    ]
    with open(p, "w") as f:
        f.write("\n".join(lines) + "\n")
    r = agent_turn_traces(p)
    check(r["n"] == 2, "T1.1 两个 assistant turn 被收")
    check(len(r["turns"]) == 2, "T1.1 turns 长度 == n")
    t0, t1 = r["turns"]
    check(t0["usageIsReal"] is True, "T1.2 turn0 非零 usage → usageIsReal=True")
    check(t0["burden"] == 6000, "T1.2 turn0 burden = input+cc = 6000 (cacheRead=9999 剔除, 非本 turn 增量)")
    check(t1["usageIsReal"] is False, "T1.3 turn1 零 usage → usageIsReal=False")
    check(t1["resultChars"] == 6000, "T1.3 turn1 resultChars = 配对 tool_result 字符数 6000")
    check(t1["burden"] == 6000, "T1.3 turn1 burden = resultChars 代理 = 6000")
    check(t1["tool"] == "Read", "T1.4 turn1 tool name = Read")
    check(t1["target"] == "src/big.py", "T1.4 turn1 target = file_path 单标签")
    # outlier: mean = (6000+6000)/2 = 6000, 阈值 1.5×6000 = 9000 → 都不超 → 无 outlier
    check(t0["outlier"] is False and t1["outlier"] is False, "T1.5 均匀 burden → 无 outlier 标记")
    # bulletproof
    check(agent_turn_traces("/nonexistent/agent-x.jsonl") == {"turns": [], "n": 0, "limit": 200000},
          "T1.6 坏路径 → empty (bulletproof)")


def test_agent_turn_traces_multi_tool():
    """A2 turn=message: 一条 assistant message 含多个 tool_use (如 bash+skill) → tag 合并显全部
    (去重保序, 重复 ×N), 不再只取首个漏 skill. 用户报: general-purpose turn0 有 bash+skill, tag 只显 bash."""
    assert agent_turn_traces is not None, "transcript_adapter.agent_turn_traces 可用"
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, "agent-multi.jsonl")
    lines = [
        # turn 0: 一 message 两 tool_use (Bash + Skill), 同 message.id → 同一 turn, tag 须显两个
        json.dumps({"type": "assistant", "timestamp": "2026-06-18T10:00:00+08:00",
                    "message": {"role": "assistant", "id": "msg-multi", "stop_reason": "tool_use",
                                "usage": {"input_tokens": 100, "cache_read_input_tokens": 0,
                                          "cache_creation_input_tokens": 0, "output_tokens": 10},
                                "content": [
                                    {"type": "tool_use", "id": "tu-bash", "name": "Bash",
                                     "input": {"command": "pip install demo-pkg"}},
                                    {"type": "tool_use", "id": "tu-skill", "name": "Skill",
                                     "input": {"skill": "demo-env-check"}},
                                ]}}),
        # turn 1: 一 message 两 Bash + 一 Read → 去重保序 + 重复 ×N
        json.dumps({"type": "assistant", "timestamp": "2026-06-18T10:00:05+08:00",
                    "message": {"role": "assistant", "id": "msg-rep", "stop_reason": "tool_use",
                                "usage": {"input_tokens": 0, "cache_read_input_tokens": 0,
                                          "cache_creation_input_tokens": 0, "output_tokens": 0},
                                "content": [
                                    {"type": "tool_use", "id": "tu-b1", "name": "Bash", "input": {"command": "ls"}},
                                    {"type": "tool_use", "id": "tu-b2", "name": "Bash", "input": {"command": "pwd"}},
                                    {"type": "tool_use", "id": "tu-r1", "name": "Read", "input": {"file_path": "a.py"}},
                                ]}}),
        # turn 2: Agent tool_use → target = subagent_type (agent 名). 用户报: root Agent turn 不显哪个 agent
        # (_short_target 旧不认 subagent_type → None → 显 '—')
        json.dumps({"type": "assistant", "timestamp": "2026-06-18T10:00:10+08:00",
                    "message": {"role": "assistant", "id": "msg-agent", "stop_reason": "tool_use",
                                "usage": {"input_tokens": 200, "cache_read_input_tokens": 0,
                                          "cache_creation_input_tokens": 0, "output_tokens": 10},
                                "content": [
                                    {"type": "tool_use", "id": "tu-ag", "name": "Agent",
                                     "input": {"subagent_type": "Explore", "description": "explore repo", "prompt": "..."}},
                                ]}}),
    ]
    with open(p, "w") as f:
        f.write("\n".join(lines) + "\n")
    r = agent_turn_traces(p)
    check(r["n"] == 3, "T-multi.1 三 message (两多-tool + 一 Agent) → 三 turn")
    t0, t1, t2 = r["turns"]
    check(t0["tool"] == "Bash · Skill", "T-multi.2 turn0 bash+skill 合并显 'Bash · Skill' (不漏 skill)")
    check(t0["target"] == "pip install demo-pkg", "T-multi.3 turn0 target = 首个 tool_use (Bash command) 单标签")
    check(t1["tool"] == "Bash ×2 · Read", "T-multi.4 turn1 去重保序 + 重复 ×N → 'Bash ×2 · Read'")
    check(t1["target"] == "ls", "T-multi.5 turn1 target = 首个 Bash command 首行")
    # per-tool 列表 (spawn 详情 turn 行每 tool_use 一行 chip, 各自带 target): 不去重, 一调用一行
    check(t0["tools"] == [{"name": "Bash", "target": "pip install demo-pkg"},
                          {"name": "Skill", "target": "demo-env-check"}],
          "T-multi.6 turn0 tools = [Bash(cmd), Skill(skill名)] (Skill target 经 _short_target 认 input.skill)")
    check(t1["tools"] == [{"name": "Bash", "target": "ls"},
                          {"name": "Bash", "target": "pwd"},
                          {"name": "Read", "target": "a.py"}],
          "T-multi.7 turn1 tools 不去重 = 3 行 (Bash×2 各自 command + Read)")
    # Agent tool_use target = subagent_type (用户报: root Agent turn 不显哪个 agent; _short_target 认 input.subagent_type)
    check(t2["tool"] == "Agent", "T-multi.8 turn2 Agent tool_use → tool name = Agent")
    check(t2["target"] == "Explore", "T-multi.9 turn2 target = subagent_type (agent 名 Explore), 不再是 None/'—'")
    check(t2["tools"] == [{"name": "Agent", "target": "Explore"}],
          "T-multi.10 turn2 per-tool chip target = Explore (description/prompt 不抢, subagent_type 优先)")


def test_agent_spawn_head():
    """Plan C T2: agent_spawn_head 重扫 root toolUseResult → spawn 头聚合 (全真, §8.6 边界1).
    fixture: root <sid>.jsonl 含一个 toolUseResult (agentId/agentType/dur/usage/toolStats/prompt)."""
    assert agent_spawn_head is not None, "transcript_adapter.agent_spawn_head 可用"
    tmp = tempfile.mkdtemp()
    sid = "aaaa1111-2222-3333-4444-555566667777"
    root_path = os.path.join(tmp, sid + ".jsonl")
    line = json.dumps({"type": "assistant", "timestamp": "2026-06-18T10:01:00+08:00",
                       "message": {"role": "assistant"},
                       "toolUseResult": {"status": "completed", "agentId": "agent-head-1",
                                         "agentType": "Explore", "totalDurationMs": 42000,
                                         "totalTokens": 9000,
                                         "usage": {"input_tokens": 1000, "cache_read_input_tokens": 7500,
                                                   "cache_creation_input_tokens": 0, "output_tokens": 500},
                                         "resolvedModel": "glm-5.1",
                                         "totalToolUseCount": 6,
                                         "toolStats": {"readCount": 4, "searchCount": 1, "bashCount": 1,
                                                       "editFileCount": 0, "linesAdded": 0, "linesRemoved": 0,
                                                       "otherToolCount": 0},
                                         "prompt": "explore the auth module and report findings"}})
    with open(root_path, "w") as f:
        f.write(line + "\n")
    h = agent_spawn_head(root_path, "agent-head-1", agent_path=None)
    check(h is not None, "T2.1 命中 agentId → head dict 非 None")
    check(h["agentType"] == "Explore", "T2.1 agentType 透传")
    check(h["totalDurationMs"] == 42000, "T2.1 dur 透传")
    tk = h["tokens"]
    check(tk["total"] == 9000 and tk["cacheRead"] == 7500, "T2.1 tokens 四桶 + total")
    # hit = cacheRead/(input+cc+cr) = 7500/(1000+0+7500) = 88.2%
    check(abs(h["hit"] - 88.2) < 0.1, f"T2.2 hit == 88.2 (input-side), got {h['hit']}")
    check(h["toolStats"]["readCount"] == 4, "T2.1 toolStats 透传 (§8.6 头全真)")
    check(h["totalToolUseCount"] == 6, "T2.1 totalToolUseCount 透传")
    # ⚠ prompt 串 "explore the auth module and report findings" 实测 43 字符 (控制器已核, 勿改)
    check(h["promptChars"] == 43, f"T2.1 promptChars == 43, got {h.get('promptChars')}")
    check(h["resolvedModel"] == "glm-5.1", "T2.1 resolvedModel 透传")
    # 未命中
    check(agent_spawn_head(root_path, "agent-nope") is None, "T2.3 未命中 agentId → None")
    # bulletproof
    check(agent_spawn_head("/nope.jsonl", "x") is None, "T2.4 坏路径 → None")


def test_agent_turn_raw():
    """Plan C T3: agent_turn_raw 读第 i 个 assistant turn 原文 (§8.6 logs, F9 on-demand).
    与 agent_turn_traces 同索引. fixture: turn0 = text+tool_use(Read), 配对 tool_result(str)."""
    assert agent_turn_raw is not None, "transcript_adapter.agent_turn_raw 可用"
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, "agent-t3.jsonl")
    lines = [
        json.dumps({"type": "assistant", "timestamp": "2026-06-18T10:00:00+08:00",
                    "message": {"role": "assistant", "stop_reason": "tool_use",
                                "usage": {"input_tokens": 800, "output_tokens": 30},
                                "content": [
                                    {"type": "text", "text": "let me read the file"},
                                    {"type": "tool_use", "id": "tu-9", "name": "Read",
                                     "input": {"file_path": "src/auth.py"}}]}}),
        json.dumps({"type": "user", "timestamp": "2026-06-18T10:00:01+08:00",
                    "message": {"role": "user", "content": [{"type": "tool_result",
                                "tool_use_id": "tu-9", "content": "def auth():\n    pass\n"}]}}),
        # turn 1 (越界目标用)
        json.dumps({"type": "assistant", "timestamp": "2026-06-18T10:00:10+08:00",
                    "message": {"role": "assistant", "stop_reason": "end_turn",
                                "content": [{"type": "text", "text": "done"}]}}),
    ]
    with open(p, "w") as f:
        f.write("\n".join(lines) + "\n")
    raw = agent_turn_raw(p, 0)
    check(raw is not None, "T3.1 turn0 → 非 None")
    check(raw["turnIndex"] == 0, "T3.1 turnIndex 透传")
    check(raw["stop_reason"] == "tool_use", "T3.1 stop_reason 透传")
    types = [b["type"] for b in raw["blocks"]]
    check(types == ["text", "tool_use"], "T3.1 blocks: text + tool_use (raw content)")
    tu = [b for b in raw["blocks"] if b["type"] == "tool_use"][0]
    check(tu["name"] == "Read" and tu["input"]["file_path"] == "src/auth.py",
          "T3.2 tool_use 全文 input 透传 (turn 原文 跨 F9 deliberately)")
    check(len(raw["results"]) == 1, "T3.3 配对 tool_result 收 1")
    check(raw["results"][0]["content"] == "def auth():\n    pass\n",
          "T3.3 tool_result raw content (str 形) 透传")
    check(raw["raw"] is True, "T3.4 raw=True 标记 (客户端显 '本地原始内容')")
    # 越界
    check(agent_turn_raw(p, 99) is None, "T3.5 越界 turnIndex → None")
    check(agent_turn_raw("/nope.jsonl", 0) is None, "T3.6 坏路径 → None")


def test_agent_turn_raw_separated():
    """T8 冒烟发现: 真 CC subagent transcript tool_use 与 tool_result 常空间分离
    (连续 assistant 各带 tool_use, tool_result 全堆后面, 非 assistant↔user 交替).
    全文索引修复: results 靠全文 tool_use_id→tool_result 索引配对, 不依赖局部 capture."""
    assert agent_turn_raw is not None
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, "agent-sep.jsonl")
    lines = [
        # 连续 assistant turn 各带 tool_use (分离结构: 无中间 user)
        json.dumps({"type": "assistant", "timestamp": "2026-06-18T10:00:00+08:00",
                    "message": {"role": "assistant", "stop_reason": "tool_use",
                                "content": [{"type": "tool_use", "id": "tu-A", "name": "Bash",
                                             "input": {"command": "ls"}}]}}),       # turn0
        json.dumps({"type": "assistant", "timestamp": "2026-06-18T10:00:01+08:00",
                    "message": {"role": "assistant", "stop_reason": "tool_use",
                                "content": [{"type": "tool_use", "id": "tu-B", "name": "Read",
                                             "input": {"file_path": "a.py"}}]}}),     # turn1
        json.dumps({"type": "assistant", "timestamp": "2026-06-18T10:00:02+08:00",
                    "message": {"role": "assistant", "stop_reason": "end_turn",
                                "content": [{"type": "text", "text": "done"}]}}),    # turn2 text-only
        # tool_result 全堆末尾 (空间分离, 局部 capture 在此结构系统性漏)
        json.dumps({"type": "user", "timestamp": "2026-06-18T10:00:03+08:00",
                    "message": {"role": "user", "content": [{"type": "tool_result",
                                "tool_use_id": "tu-A", "content": "file1\nfile2"}]}}),
        json.dumps({"type": "user", "timestamp": "2026-06-18T10:00:04+08:00",
                    "message": {"role": "user", "content": [{"type": "tool_result",
                                "tool_use_id": "tu-B", "content": "print('hi')"}]}}),
    ]
    with open(p, "w") as f:
        f.write("\n".join(lines) + "\n")
    # turn1 = tool_use tu-B, 其 result 在原文 ~3 行后 (分离结构, 局部 capture 必漏)
    r1 = agent_turn_raw(p, 1)
    check(r1 is not None, "SEP turn1 found")
    check(len(r1["blocks"]) == 1 and r1["blocks"][0]["name"] == "Read", "SEP turn1 blocks=Read tool_use")
    check(len(r1["results"]) == 1, "SEP turn1 results=1 (全文索引配对, 非局部 capture)")
    check(r1["results"][0]["content"] == "print('hi')", "SEP turn1 result content 透传")
    # turn0 = tool_use tu-A
    r0 = agent_turn_raw(p, 0)
    check(len(r0["results"]) == 1 and r0["results"][0]["content"] == "file1\nfile2",
          "SEP turn0 result 配对 (tu-A)")
    # turn2 = text only, 无 tool_use → results 空
    r2 = agent_turn_raw(p, 2)
    check(len(r2["results"]) == 0, "SEP turn2 (text only) results=0")
    # 越界
    check(agent_turn_raw(p, 99) is None, "SEP 越界 → None")


def test_spawn_route():
    """D8: scan 源 server → GET /api/spawn/<sid>/<agentId> 返 {head, traces, depth2Note}.
    fixture: root <sid>.jsonl (含 toolUseResult agentId=agent-d8) + <sid>/subagents/agent-agent-d8.jsonl (含 turn)."""
    tmp = tempfile.mkdtemp()
    proj = os.path.join(tmp, "-home-fakeproj")
    sid = "bbbb1111-2222-3333-4444-555566667777"
    sdir = os.path.join(proj, sid, "subagents")
    os.makedirs(sdir, exist_ok=True)
    root_path = os.path.join(proj, sid + ".jsonl")
    with open(root_path, "w") as f:
        f.write(json.dumps({"type": "assistant", "timestamp": "2026-06-18T10:00:00+08:00",
                            "sessionId": sid, "message": {"role": "assistant"},
                            "toolUseResult": {"status": "completed", "agentId": "agent-d8",
                                              "agentType": "Explore", "totalDurationMs": 5000,
                                              "totalTokens": 6000,
                                              "usage": {"input_tokens": 1000, "cache_read_input_tokens": 4000,
                                                        "cache_creation_input_tokens": 0, "output_tokens": 200},
                                              "totalToolUseCount": 1,
                                              "toolStats": {"readCount": 1}}}) + "\n")
    with open(os.path.join(sdir, "agent-agent-d8.jsonl"), "w") as f:
        f.write(json.dumps({"type": "assistant", "timestamp": "2026-06-18T10:00:01+08:00",
                            "message": {"role": "assistant", "stop_reason": "tool_use",
                                        "usage": {"input_tokens": 500, "cache_read_input_tokens": 3000,
                                                  "output_tokens": 10},
                                        "content": [{"type": "tool_use", "id": "tu-d8", "name": "Read",
                                                     "input": {"file_path": "x.py"}}]}}) + "\n")
    port = _free_port()
    proc = _start(port, f"scan:{tmp}")
    try:
        check(_wait_ready(port), "D8 server ready (scan source)")
        status, body = _get(port, f"/api/spawn/{sid}/agent-d8")
        check(status == 200, "D8 /api/spawn/<sid>/<agentId> 200")
        got = json.loads(body)
        check(got.get("head", {}).get("agentType") == "Explore", "D8 head.agentType")
        check(got.get("head", {}).get("totalToolUseCount") == 1, "D8 head.totalToolUseCount")
        tr = got.get("traces") or {}
        check(tr.get("n") == 1, "D8 traces.n == 1")
        check(got.get("depth2Note"), "D8 depth2Note 在 (§8.6 边界4)")
        # 不存在 agentId → 404 (agent 文件缺)
        s404, _ = _get(port, f"/api/spawn/{sid}/agent-nope")
        check(s404 == 404, "D8 不存在 agentId → 404")
    finally:
        proc.terminate(); proc.wait()


def test_turn_route():
    """D9: scan 源 server → GET /api/turn/<sid>/<agentId>/<i> 返 raw turn (turn 原文 logs, F9 on-demand)."""
    tmp = tempfile.mkdtemp()
    proj = os.path.join(tmp, "-home-fakeproj")
    sid = "cccc1111-2222-3333-4444-555566667777"
    sdir = os.path.join(proj, sid, "subagents")
    os.makedirs(sdir, exist_ok=True)
    with open(os.path.join(proj, sid + ".jsonl"), "w") as f:
        f.write(json.dumps({"type": "assistant", "timestamp": "2026-06-18T10:00:00+08:00",
                            "sessionId": sid, "message": {"role": "assistant"},
                            "toolUseResult": {"status": "completed", "agentId": "agent-d9",
                                              "agentType": "Explore", "totalTokens": 100,
                                              "usage": {"input_tokens": 100}}}) + "\n")
    with open(os.path.join(sdir, "agent-agent-d9.jsonl"), "w") as f:
        f.write(json.dumps({"type": "assistant", "timestamp": "2026-06-18T10:00:01+08:00",
                            "message": {"role": "assistant", "stop_reason": "tool_use",
                                        "usage": {"input_tokens": 200},
                                        "content": [{"type": "text", "text": "hi"},
                                                    {"type": "tool_use", "id": "tu-d9", "name": "Bash",
                                                     "input": {"command": "ls"}}]}}) + "\n")
        f.write(json.dumps({"type": "user", "timestamp": "2026-06-18T10:00:02+08:00",
                            "message": {"role": "user", "content": [{"type": "tool_result",
                                        "tool_use_id": "tu-d9", "content": "a.py\nb.py"}]}}) + "\n")
    port = _free_port()
    proc = _start(port, f"scan:{tmp}")
    try:
        check(_wait_ready(port), "D9 server ready")
        status, body = _get(port, f"/api/turn/{sid}/agent-d9/0")
        check(status == 200, "D9 /api/turn/.../0 200")
        got = json.loads(body)
        check(got.get("raw") is True, "D9 raw 标记 (客户端显 本地原始内容)")
        check(any(b["type"] == "tool_use" for b in got.get("blocks", [])), "D9 blocks 含 tool_use")
        check(len(got.get("results", [])) == 1, "D9 results 含 1 配对 tool_result")
        # 越界 turnIndex → 404
        s404, _ = _get(port, f"/api/turn/{sid}/agent-d9/99")
        check(s404 == 404, "D9 越界 turnIndex → 404")
    finally:
        proc.terminate(); proc.wait()


def test_turn_route_root():
    """D5: GET /api/turn/<sid>/root/<i> — root 主线 (orchestrator) turn 也可 钻取.
    agent_id=="root" → server 用 root transcript (跳过 _agent_path subagents 派生). root jsonl 自带
    assistant content (text+tool_use) → agent_turn_raw 取回 blocks/results (镜像 test_turn_route 但 root)."""
    tmp = tempfile.mkdtemp()
    proj = os.path.join(tmp, "-home-fakeproj")
    sid = "eeee1111-2222-3333-4444-555566667777"
    os.makedirs(proj, exist_ok=True)
    # root jsonl: 一个普通 root assistant turn (text + tool_use), 非 spawn — root 主线本身可观测
    with open(os.path.join(proj, sid + ".jsonl"), "w") as f:
        f.write(json.dumps({"type": "assistant", "timestamp": "2026-06-18T10:00:01+08:00",
                            "message": {"role": "assistant", "stop_reason": "tool_use",
                                        "usage": {"input_tokens": 200},
                                        "content": [{"type": "text", "text": "planning root turn"},
                                                    {"type": "tool_use", "id": "tu-root", "name": "Bash",
                                                     "input": {"command": "ls"}}]}}) + "\n")
        f.write(json.dumps({"type": "user", "timestamp": "2026-06-18T10:00:02+08:00",
                            "message": {"role": "user", "content": [{"type": "tool_result",
                                        "tool_use_id": "tu-root", "content": "a.py\nb.py"}]}}) + "\n")
    port = _free_port()
    proc = _start(port, f"scan:{tmp}")
    try:
        check(_wait_ready(port), "D5 server ready")
        status, body = _get(port, f"/api/turn/{sid}/root/0")
        check(status == 200, f"D5 /api/turn/<sid>/root/0 200, got {status}")
        got = json.loads(body)
        check(got.get("agentId") == "root", "D5 返 agentId=root sentinel (root 主线 turn)")
        check(got.get("raw") is True, "D5 raw 标记 (本地原始内容)")
        check(any(b["type"] == "tool_use" for b in got.get("blocks", [])), "D5 blocks 含 tool_use")
        check(len(got.get("results", [])) == 1, "D5 results 含 1 配对 tool_result")
        # 越界 turnIndex → 404 (agent_turn_raw 返 None → 404, 不抛)
        s404, _ = _get(port, f"/api/turn/{sid}/root/99")
        check(s404 == 404, "D5 root 越界 turnIndex → 404")
    finally:
        proc.terminate(); proc.wait()


def test_source_switch():
    """D13: 运行时 source 切换 (POST /api/source). server file:fx1(200) → POST file:fx2(999) →
    GET /api/source 反映新值 + /api/result 渲染新源. §8 dashboard 运行时切数据源 (非 --source 启动期固定)."""
    fx1 = _fixture()  # total=200
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(fx1, f); p1 = f.name
    fx2 = _fixture(); fx2["grandTotal"]["total"] = 999
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(fx2, f); p2 = f.name
    port = _free_port()
    proc = _start(port, f"file:{p1}")
    try:
        check(_wait_ready(port), "D13 server ready (file:fx1)")
        _, b0 = _get(port, "/api/result")
        check(json.loads(b0)["grandTotal"]["total"] == 200, "D13 初始 total == 200 (fx1)")
        # 运行时切到 fx2 (不重启 server)
        s, body = _post(port, "/api/source", {"source": f"file:{p2}"})
        check(s == 200, f"D13 POST /api/source 200, got {s}")
        check(json.loads(body).get("current") == f"file:{p2}", "D13 POST 返 current == file:fx2")
        # GET /api/source 反映新值
        s2, b2 = _get(port, "/api/source")
        check(s2 == 200, "D13 GET /api/source 200")
        check(json.loads(b2).get("current") == f"file:{p2}", "D13 GET /api/source current == file:fx2")
        # /api/result 渲染新源 (total=999)
        _, b3 = _get(port, "/api/result")
        check(json.loads(b3)["grandTotal"]["total"] == 999, "D13 切后 /api/result total == 999 (新源)")
    finally:
        proc.terminate(); proc.wait(); os.unlink(p1); os.unlink(p2)


def test_source_switch_invalid():
    """D14: 非法 source → 400 + SOURCE 不变 + 旧缓存保留 (atomic validate-first).
    transcript:/nonexistent 路径不存在 → 校验拒, 绝不污染 SOURCE/缓存."""
    fx1 = _fixture()  # total=200
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(fx1, f); p1 = f.name
    port = _free_port()
    proc = _start(port, f"file:{p1}")
    try:
        check(_wait_ready(port), "D14 server ready (file:fx1)")
        s, body = _post(port, "/api/source", {"source": "transcript:/nonexistent/path.jsonl"})
        check(s == 400, f"D14 非法 source → 400, got {s}")
        check("error" in json.loads(body), "D14 400 body 含 error")
        # SOURCE 未变 (仍 file:fx1)
        _, b2 = _get(port, "/api/source")
        check(json.loads(b2).get("current") == f"file:{p1}", "D14 GET /api/source current 仍 file:fx1 (未污染)")
        # 旧缓存保留 (total=200)
        _, b3 = _get(port, "/api/result")
        check(json.loads(b3)["grandTotal"]["total"] == 200, "D14 /api/result total 仍 200 (旧缓存保留)")
        # D14+: 裸不存在 path → 400 (auto-infer 路径也守 validate-first, SOURCE 不污染)
        s4, b4 = _post(port, "/api/source", {"source": "/no/such/path-xyz-999"})
        check(s4 == 400, f"D14 裸不存在 path → 400 (auto-infer 拒), got {s4}")
        _, b5 = _get(port, "/api/source")
        check(json.loads(b5).get("current") == f"file:{p1}", "D14 auto-infer 拒后 SOURCE 仍 file:fx1 (未污染)")
    finally:
        proc.terminate(); proc.wait(); os.unlink(p1)


def test_source_switch_frontend_contract():
    """D15: 前端 source 切换器契约 (运行时切数据源 UI). JS 难单测 → 测契约锚点:
    index.html 含 #source-select/#source-input/#source-apply;
    app.js 含 initSourceSwitcher (def+call) + POST 方法 + /api/source 路径."""
    html = open(os.path.join(HERE, "..", "dashboard", "static", "index.html")).read()
    appjs = open(os.path.join(HERE, "..", "dashboard", "static", "app.js")).read()
    for eid in ["source-select", "source-input", "source-apply"]:
        check(f'id="{eid}"' in html, f"D15 index.html 含 #{eid}")
    check(appjs.count("initSourceSwitcher") >= 2, "D15 app.js 定义 + 调用 initSourceSwitcher (def + call)")
    check('"POST"' in appjs or "'POST'" in appjs, "D15 app.js 含 POST 方法 (fetch)")
    check("/api/source" in appjs, "D15 app.js 引用 /api/source 路径")


def test_browse_endpoint():
    """D16: GET /api/browse?dir=X — server 读真实 FS 返目录 + .jsonl 文件列表 (前端弹层导航).
    浏览器原生 <input type=file> 拿不到真实路径 (fakepath 安全铁律) → 走 server 读 FS 正路.
    安全: BROWSE_ROOT (env AGENTINSIGHT_BROWSE_ROOT, 默认 home) 可信根 + realpath 路径穿越防护."""
    tmp = tempfile.mkdtemp()
    os.makedirs(os.path.join(tmp, "proj-a"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "proj-a", "sub"), exist_ok=True)
    with open(os.path.join(tmp, "proj-a", "sess1.jsonl"), "w") as f:
        f.write("{}\n")
    with open(os.path.join(tmp, "readme.txt"), "w") as f:   # 非 .jsonl → 须过滤
        f.write("hi\n")
    fx = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)   # source 用 file (browse 与 source 解耦)
    json.dump(_fixture(), fx); fx.close(); fxp = fx.name
    os.environ["AGENTINSIGHT_BROWSE_ROOT"] = tmp
    port = _free_port()
    proc = _start(port, f"file:{fxp}")
    try:
        check(_wait_ready(port), "D16 server ready")
        # D16a: 默认根 (无 dir) → 根内容
        s, body = _get(port, "/api/browse")
        check(s == 200, f"D16a GET /api/browse (默认根) 200, got {s}")
        d = json.loads(body)
        check(d["dir"] == tmp, "D16a dir == BROWSE_ROOT")
        check(d.get("parent") is None, "D16a parent == null (根本身无上级)")
        names = {e["name"] for e in d["entries"]}
        check("proj-a" in names, "D16a entries 含子目录 proj-a")
        check("readme.txt" not in names, "D16a 过滤非 .jsonl 文件 (readme.txt 不返回)")
        # D16b: 子目录 dir → 内容 + parent=根
        s2, b2 = _get(port, f"/api/browse?dir={tmp}/proj-a")
        check(s2 == 200, f"D16b 子目录 dir 200, got {s2}")
        d2 = json.loads(b2)
        check(d2.get("parent") == tmp, "D16b parent == 根")
        names2 = {e["name"] for e in d2["entries"]}
        check("sess1.jsonl" in names2, "D16b entries 含 .jsonl 文件")
        check("sub" in names2, "D16b entries 含子目录 sub")
        fe = [e for e in d2["entries"] if e["name"] == "sess1.jsonl"][0]
        check(fe["isDir"] is False, "D16b .jsonl 文件 isDir=False")
        check(fe.get("isJsonl") is True, "D16b .jsonl 文件 isJsonl=True")
        # D16c: 路径穿越 — 可信根外 (/etc)
        s3, _ = _get(port, "/api/browse?dir=/etc")
        check(s3 == 400, f"D16c 根外路径 /etc → 400 (可信根防护), got {s3}")
        # D16d: dir 含 .. 逃逸根
        s4, _ = _get(port, f"/api/browse?dir={tmp}/..")
        check(s4 == 400, f"D16d dir 含 .. 逃逸根 → 400, got {s4}")
        # D16e: 不存在目录
        s5, _ = _get(port, f"/api/browse?dir={tmp}/nope-xyz")
        check(s5 == 400, f"D16e 不存在 dir → 400, got {s5}")
        # D16f: dir 是文件非目录
        s6, _ = _get(port, f"/api/browse?dir={tmp}/proj-a/sess1.jsonl")
        check(s6 == 400, f"D16f dir 是文件非目录 → 400, got {s6}")
    finally:
        proc.terminate(); proc.wait()
        os.environ.pop("AGENTINSIGHT_BROWSE_ROOT", None)
        os.unlink(fxp)


def test_browse_frontend_contract():
    """D17: 前端目录浏览弹层契约. 浏览器 fakepath 铁律 → server /api/browse + 自建弹层.
    index.html 含弹层骨架 id; app.js 含 initBrowser (def+call) + /api/browse 路径."""
    html = open(os.path.join(HERE, "..", "dashboard", "static", "index.html")).read()
    appjs = open(os.path.join(HERE, "..", "dashboard", "static", "app.js")).read()
    for eid in ["browse-modal", "browse-path", "browse-list", "browse-select"]:
        check(f'id="{eid}"' in html, f"D17 index.html 含 #{eid}")
    check(appjs.count("initBrowser") >= 2, "D17 app.js 定义 + 调用 initBrowser (def + call)")
    check("/api/browse" in appjs, "D17 app.js 引用 /api/browse 路径")
    check("browse-modal" in appjs, "D17 app.js 引用 #browse-modal")
    check("browse-kind" not in html, "D17 index.html 去掉 browse-kind 源类型单选块 (类型自动识别)")


def test_infer_source():
    """D18: 裸 path → source 自动推断 (server._infer_source, 类型判断移到代码 · 用户只选目录/文件或粘贴).
    带 prefix / 裸 scan|live → 原样; 裸目录 → scan (在 live logdir 基下 → live); 裸 .jsonl → transcript; 其他 → 拒."""
    sys.path.insert(0, os.path.join(HERE, "..", "dashboard"))
    import server
    tmp = tempfile.mkdtemp()
    try:
        # 1. 裸 scan / live → 原样
        s, e = server._infer_source("scan")
        check(s == "scan" and e is None, "D18 裸 scan → ('scan', None)")
        s, e = server._infer_source("live")
        check(s == "live" and e is None, "D18 裸 live → ('live', None)")
        # 2. 带 prefix → 原样 (向后兼容, 高级用户/预置项仍可用 prefix)
        s, e = server._infer_source("scan:/some/dir")
        check(s == "scan:/some/dir", "D18 scan:DIR 带前缀原样")
        s, e = server._infer_source("transcript:/x.jsonl")
        check(s == "transcript:/x.jsonl", "D18 transcript:PATH 带前缀原样")
        # 3. 裸存在目录 (非 live logdir) → scan:<realpath>
        s, e = server._infer_source(tmp)
        check(s == "scan:" + os.path.realpath(tmp) and e is None, "D18 裸目录 → scan:<realpath>")
        # 4. 裸目录 == live logdir (设 env, call-time 读) → live:<realpath>; 其下子目录亦 live (record.py 按 <base>/<proj>/ 滚动)
        os.environ["AGENTINSIGHT_LOG_DIR"] = tmp
        try:
            s, e = server._infer_source(tmp)
            check(s == "live:" + os.path.realpath(tmp) and e is None,
                  "D18 裸 live logdir → live:<realpath> (call-time env 生效)")
            sub = os.path.join(tmp, "some-proj")
            os.makedirs(sub, exist_ok=True)
            s, e = server._infer_source(sub)
            check(s == "live:" + os.path.realpath(sub), "D18 live logdir 下子目录 → live:<realpath>")
        finally:
            os.environ.pop("AGENTINSIGHT_LOG_DIR", None)
        # 5. 裸 .jsonl 文件 → transcript:<realpath>
        jf = os.path.join(tmp, "sess.jsonl")
        with open(jf, "w") as f:
            f.write("{}\n")
        s, e = server._infer_source(jf)
        check(s == "transcript:" + os.path.realpath(jf) and e is None, "D18 裸 .jsonl → transcript:<realpath>")
        os.unlink(jf)
        # 6. 裸非 .jsonl 文件 → 拒
        tf = os.path.join(tmp, "readme.txt")
        with open(tf, "w") as f:
            f.write("hi\n")
        s, e = server._infer_source(tf)
        check(s is None and "unsupported" in e, "D18 裸非 .jsonl 文件 → 拒")
        os.unlink(tf)
        # 7. 裸不存在 → 拒
        s, e = server._infer_source(os.path.join(tmp, "nope-xyz"))
        check(s is None and "path not found" in e, "D18 裸不存在 → 拒")
        # 8. 空 source → 拒
        s, e = server._infer_source("")
        check(s is None and e == "missing/empty source", "D18 空 source → 拒")
    finally:
        pass   # tmp 留 /tmp (同 D16 范式, 无需清理)


def test_source_autoinfer_frontend():
    """D19: 前端自动推断契约 (类型判断移到代码 · 去术语). index.html 去 __custom__/browse-kind/静态 option;
    app.js 有 initPresets (def+call) + /api/presets + mode chip 三态 (live-tail 开关 × 数据活性: ●实时/⏳静止/⏸暂停;
    _liveTailOn + _lastDataActive 据 result.dataAgeSeconds<STALE_AFTER_S 驱动; 不再用 isLive) + initBrowser 去 setKind/kindValue."""
    html = open(os.path.join(HERE, "..", "dashboard", "static", "index.html")).read()
    appjs = open(os.path.join(HERE, "..", "dashboard", "static", "app.js")).read()
    css = open(os.path.join(HERE, "..", "dashboard", "static", "style.css")).read()
    check("__custom__" not in html, "D19 index.html 去掉 __custom__ 选项")
    check("browse-kind" not in html, "D19 index.html 去掉 browse-kind 源类型单选块")
    check('<option value="scan">' not in html, "D19 index.html #source-select 无静态 option (JS 动态填)")
    check(appjs.count("initPresets") >= 2, "D19 app.js 定义 + 调用 initPresets (def + call)")
    check("/api/presets" in appjs, "D19 app.js 引用 /api/presets 路径")
    # mode chip 三态: 刷新轴 (live-tail 开关) × 数据活性 (_lastDataActive 据 result.dataAgeSeconds < STALE_AFTER_S) ——
    # 开 且 源在动 → ●实时 / 开 但 源长期静止 (旧 session 不再活动) → ⏳静止 / 关 → ⏸暂停. 不再标来源轴 isLive ——
    # 用户反馈: "实时不实时是 live-tail 给的, 跟读哪个文件源无关"; 后续补: 选旧 session 时 live-tail 开着也该显静止 (非误导成实时).
    check("friendlyMode" not in appjs, "D19 app.js 删除 friendlyMode (旧四态友好化函数)")
    check("_liveTailOn" in appjs and "updateLiveChip" in appjs and "modeChipState" in appjs,
          "D19 app.js mode chip 由 _liveTailOn/_lastDataActive 驱动 + modeChipState 三态 + updateLiveChip 切换瞬间更新")
    check("_lastDataActive" in appjs and "STALE_AFTER_S" in appjs,
          "D19 app.js 数据活性变量 _lastDataActive + 静止阈值常量 STALE_AFTER_S")
    check("● 实时" in appjs and "⏸ 暂停" in appjs and "⏳ 静止" in appjs,
          "D19 app.js mode chip 三态标签 ● 实时 / ⏳ 静止 / ⏸ 暂停")
    check(".chip.live" in css and ".chip.paused" in css and ".chip.stale" in css,
          "D19 style.css 三态样式 .chip.live (绿) / .chip.paused (橙) / .chip.stale (灰)")
    check(appjs.count("initBrowser") >= 2, "D19 app.js 仍定义 + 调用 initBrowser (弹层骨架保留)")
    check("setKind" not in appjs and "kindValue" not in appjs,
          "D19 app.js initBrowser 去掉 kindValue/setKind (类型自动推断, 不暴露给用户)")
    check("PRESETS" not in appjs, "D19 app.js 去掉 PRESETS 集合 (无预设/自定义概念)")


def test_session_drill_transcript():
    """D21: transcript 单文件源 → /api/session/<sid> 200 (drill session 不再卡 'no scanDir').
    transcript 源无 scanDir, 但 root path = SOURCE 的 transcript 文件本身 (fast-path), 无需反查.
    session/spawn/turn 共用 _resolve_root_path, 一处修好全通."""
    import json as _json
    tmp = tempfile.mkdtemp()
    sid = "ddddeeee-1111-2222-3333-444455556666"
    root_path = os.path.join(tmp, sid + ".jsonl")
    lines = [
        # assistant usage turn → rootContext.samples (ctx 3000+6000=9000)
        _json.dumps({"type": "assistant", "timestamp": "2026-06-18T10:00:00+08:00",
                    "message": {"role": "assistant",
                                "usage": {"input_tokens": 3000, "cache_creation_input_tokens": 0,
                                          "cache_read_input_tokens": 6000, "output_tokens": 100}}}),
        # Agent spawn 行 → callChains (1 spawn)
        _json.dumps({"timestamp": "2026-06-18T10:01:00+08:00", "sessionId": sid, "isSidechain": False,
                    "type": "assistant", "uuid": "u-1", "message": {"role": "assistant"},
                    "toolUseResult": {"status": "completed", "agentId": "agent-1", "agentType": "Explore",
                                      "totalDurationMs": 5000, "totalTokens": 5050,
                                      "usage": {"input_tokens": 500, "output_tokens": 50,
                                                "cache_creation_input_tokens": 0,
                                                "cache_read_input_tokens": 4500}}}),
    ]
    with open(root_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    port = _free_port()
    proc = _start(port, f"transcript:{root_path}")   # transcript 单文件源 (无 scanDir)
    try:
        check(_wait_ready(port), "D21 server ready (transcript source)")
        status, body = _get(port, f"/api/session/{sid}")
        check(status == 200, f"D21 transcript 源 /api/session/<sid> 200 (drill 不再卡 no scanDir), got {status}")
        got = _json.loads(body)
        check(len(got.get("callChains", [])) >= 1, "D21 transcript 源 callChains 非空 (≥1 spawn)")
        rc = got.get("rootContext") or {}
        check(rc.get("peak") == 9000, f"D21 transcript 源 rootContext.peak == 9000, got {rc.get('peak')}")
        # sid 不匹配当前 transcript 文件 → 404 (不返回错数据)
        s404, _ = _get(port, "/api/session/nope-nope-nope")
        check(s404 == 404, f"D21 transcript 源 sid 不匹配文件 → 404, got {s404}")
    finally:
        proc.terminate(); proc.wait()


def test_hero_panel_ux():
    """D20: hero 面板交互 + 去内部术语契约 (app.js 文本锚点).
    行可点→session 视图 / 'more'→跳总览表(非展开) / ctxPeak 旁 output 伴随 / 去用户可见内部词 (§/Plan/model ceiling)."""
    appjs = open(os.path.join(HERE, "..", "dashboard", "static", "app.js")).read()
    css = open(os.path.join(HERE, "..", "dashboard", "static", "style.css")).read()
    # --- hero 行可点 → drillSession (fleet→session 视图 钻取延伸到 hero) ---
    check('data-sid=' in appjs, "D20 hero dist-row 带 data-sid (每根柱=session, 可点)")
    check(appjs.count("initHeroClicks") >= 2, "D20 app.js 定义 + 调用 initHeroClicks (hero 行点击委托, 父级一次性挂载)")
    check("drillSession" in appjs, "D20 hero 行点击 → drillSession (进 session 视图)")
    # --- '…N more' → 跳总览表 + flash (非原地展开; hero 聚光灯 / 表花名册分工) ---
    check("dist-more" in appjs, "D20 '…N more' 行标 dist-more (跳转入口)")
    check("jumpToFleetTable" in appjs, "D20 '…N more' → 调 jumpToFleetTable (非原地展开)")
    check("scrollIntoView" in appjs, "D20 more 点击 → scrollIntoView 滚到总览表")
    check("fleet-table" in appjs and appjs.count("flash") >= 2, "D20 more 跳转后 flash 高亮总览表 (add+remove flash)")
    check("fleet-flash" in css and "#fleet-table.flash" in css, "D20 style.css 有 fleet-flash 动画 + #fleet-table.flash 规则")
    # --- context 面板每根柱补 output 伴随 (grandTotal.output 现成字段, 非并入 ctxPeak) ---
    check(appjs.count("grandTotal") >= 1 and "output" in appjs, "D20 ctxPeak 旁补 output (grandTotal.output)")
    check("fmtK" in appjs, "D20 output 紧凑格式 fmtK (k/M)")
    check("ctx-out" in appjs and ".ctx-out" in css, "D20 output 伴随用 .ctx-out (app.js 标记 + style.css 样式)")
    check("output 单列" in appjs, "D20 context 口径说明含 'output 单列' (output 旁注, 非并入峰值)")
    # --- 去用户可见内部术语 (§章节号 / Plan 3a / model ceiling) → 中文友好 ---
    check("model ceiling" not in appjs, "D20 去掉 'model ceiling' 内部词 (改中文 '模型上限')")
    check("非逼近模型上限" in appjs, "D20 'model ceiling' → '非逼近模型上限' 友好化 (comment + title)")
    check("§9.3#" not in appjs, "D20 用户可见串去 §9.3#x 内部引用 (rootContext/depth-2/provider artifact 三处)")
    check("(§8.4)" not in appjs, "D20 gantt note 去 (§8.4) 内部引用")
    check("§8.11.3 回链" not in appjs, "D20 skill 回链标题去 §8.11.3 内部引用")


def test_session_ctx_peak_transcript():
    """D22: transcript 源 perSession[0].ctxPeak 填真实 root 主线峰值 (问题3修复; 对齐 _mode_b §8.3 口径).
    修复前 _mode_a_result 写死 ctx_peak=0 → 单 session context 面板 'ctxPeak 全 0'; live/jsonl 源仍 0 (契约不破, 见 test_analyze 组12)."""
    import json as _json
    tmp = tempfile.mkdtemp()
    sid = "ddddeeee-1111-2222-3333-444455556666"
    root_path = os.path.join(tmp, sid + ".jsonl")
    lines = [
        # assistant usage turn → root 主线 ctx 峰值 (input 3000 + cacheRead 6000 = 9000)
        _json.dumps({"type": "assistant", "timestamp": "2026-06-18T10:00:00+08:00",
                    "message": {"role": "assistant",
                                "usage": {"input_tokens": 3000, "cache_creation_input_tokens": 0,
                                          "cache_read_input_tokens": 6000, "output_tokens": 100}}}),
        # Agent spawn 行 (sessionId=sid → perSession 归属 root sid, 命中 _root_sid 精确归属)
        _json.dumps({"timestamp": "2026-06-18T10:01:00+08:00", "sessionId": sid, "isSidechain": False,
                    "type": "assistant", "uuid": "u-1", "message": {"role": "assistant"},
                    "toolUseResult": {"status": "completed", "agentId": "agent-1", "agentType": "Explore",
                                      "totalDurationMs": 5000, "totalTokens": 5050,
                                      "usage": {"input_tokens": 500, "output_tokens": 50,
                                                "cache_creation_input_tokens": 0,
                                                "cache_read_input_tokens": 4500}}}),
    ]
    with open(root_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    port = _free_port()
    proc = _start(port, f"transcript:{root_path}")
    try:
        check(_wait_ready(port), "D22 server ready (transcript source)")
        s, body = _get(port, "/api/result")
        check(s == 200, f"D22 /api/result 200, got {s}")
        res = _json.loads(body)
        r = res.get("result", res)
        ps = r.get("perSession") or []
        check(len(ps) >= 1, f"D22 transcript 源 perSession 非空, got {len(ps)}")
        rc_peak = (r.get("rootContext") or {}).get("peak")
        got_peak = ps[0].get("ctxPeak")
        check(got_peak == 9000, f"D22 perSession[0].ctxPeak 填真实峰值 9000 (问题3修复), got {got_peak}")
        check(got_peak == rc_peak, f"D22 perSession[0].ctxPeak == rootContext.peak (口径一致), got {got_peak} vs {rc_peak}")
        check(got_peak > 0, "D22 perSession[0].ctxPeak > 0 (修复前 _mode_a 写死 0)")
    finally:
        proc.terminate(); proc.wait()


def test_trust_single_session_fallback():
    """D23: trust 栏 = fleet 异常信号 (§8.3 三类计数: 💥爆掉/⚠低命中/⏳异步未回报).
    多/单 session 同一套 (单 session blown/lowHit 为 0/1); 旧 'session self-consistent'/'ps0.consistent' isRoot 不变量回退已撤 (恒真无业务信息)."""
    appjs = open(os.path.join(HERE, "..", "dashboard", "static", "app.js")).read()
    check("scanConsistency N/A" not in appjs, "D23 去掉 'scanConsistency N/A' 后端术语 (banner 不再露)")
    check("single-session mode" not in appjs, "D23 去掉 'single-session mode' 英文术语占位")
    check("session self-consistent" not in appjs, "D23 旧 isRoot 自洽文案 'session self-consistent' 已撤 (恒真无业务信息)")
    check("ps0.consistent" not in appjs, "D23 旧单 session ps0.consistent 回退已撤 (统一走 fleet 异常计数)")
    check("0 异常" in appjs, "D23 新 banner 全 0 绿文案 '0 异常' (§8.3 fleet 健康信号)")
    check("asyncCount" in appjs, "D23 新 banner 含 asyncCount (⏳ 异步未回报计数)")


def test_source_is_live_and_inject():
    """D24: _source_is_live 分类 (来源轴) + /api/result 注入 isLive (来源轴真值, CLI/诊断用; 顶部 chip 已改刷新轴不再消费, 见 D19).
    live/live: → True; scan/transcript/jsonl/file → False. isLive 标的是数据源 (record.py JSONL vs transcript), 非"是否实时"——
    实时性由 live-tail 开关 (刷新轴) 决定, 与来源轴正交. e2e: transcript 源 /api/result.isLive==False."""
    sys.path.insert(0, os.path.join(HERE, "..", "dashboard"))
    import server
    # --- _source_is_live 分类 (来源轴: live → True; 其余 → False) ---
    check(server._source_is_live("live") is True, "D24 live → True (实时)")
    check(server._source_is_live("live:/tmp/xx") is True, "D24 live:DIR → True")
    check(server._source_is_live("scan") is False, "D24 scan → False (离线)")
    check(server._source_is_live("scan:/tmp/xx") is False, "D24 scan:DIR → False")
    check(server._source_is_live("transcript:/x/y.jsonl") is False, "D24 transcript:FILE → False")
    check(server._source_is_live("jsonl:/x/y.jsonl") is False, "D24 jsonl:FILE → False")
    check(server._source_is_live("file:/x/y.json") is False, "D24 file:FILE → False")
    # --- e2e: transcript 源 /api/result 注入 isLive == False (来源轴: 非 live 源; 浅 copy 不污染 STATE 缓存) ---
    import json as _json
    tmp = tempfile.mkdtemp()
    sid = "ddeeff00-1111-2222-3333-444455556666"
    root_path = os.path.join(tmp, sid + ".jsonl")
    lines = [
        _json.dumps({"type": "assistant", "timestamp": "2026-06-19T10:00:00+08:00",
                    "message": {"role": "assistant",
                                "usage": {"input_tokens": 1000, "cache_creation_input_tokens": 0,
                                          "cache_read_input_tokens": 0, "output_tokens": 50}}}),
    ]
    with open(root_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    port = _free_port()
    proc = _start(port, f"transcript:{root_path}")
    try:
        check(_wait_ready(port), "D24 server ready (transcript source)")
        s, body = _get(port, "/api/result")
        check(s == 200, f"D24 /api/result 200, got {s}")
        res = _json.loads(body)
        check("isLive" in res, "D24 /api/result 顶层带 isLive 字段 (server 注入)")
        check(res.get("isLive") is False, f"D24 transcript 源 /api/result.isLive == False (离线), got {res.get('isLive')}")
        check("dataAgeSeconds" in res, "D24 /api/result 顶层带 dataAgeSeconds 字段 (数据活性, server 注入)")
        check(res.get("dataAgeSeconds") is not None and res.get("dataAgeSeconds") >= 0,
              f"D24 /api/result.dataAgeSeconds 非负 (源文件刚写, 应小), got {res.get('dataAgeSeconds')}")
    finally:
        proc.terminate(); proc.wait()


def test_logdir_delete_and_data_age():
    """D26: _logdir_changed 跟删除 (删文件 mtime 不增, 旧版单比 mtime 漏 → 2026-06-21 删盲区补丁: 并比文件集) + _watch_data_age 活性信号 (前端 chip 实时/静止据此).
    手动建基线 (_LAST_REFRESH_MTIME/_LAST_WATCH_FILES, 不跑 _refresh 免触发 analyze scan):
    无变 → False; 删一文件 → 集合变 → True (删盲区); 增一文件 → True; _watch_data_age 无文件→None, 有文件→非负."""
    sys.path.insert(0, os.path.join(HERE, "..", "dashboard"))
    import server
    tmp = tempfile.mkdtemp()
    f1 = os.path.join(tmp, "aaa.jsonl")
    f2 = os.path.join(tmp, "bbb.jsonl")
    with open(f1, "w") as fh: fh.write("{}\n")
    with open(f2, "w") as fh: fh.write("{}\n")
    src = "scan:" + tmp
    sv_m, sv_w = server._LAST_REFRESH_MTIME, server._LAST_WATCH_FILES   # 存档全局基线 (in-process import 共享, finally 还原防污染他测)
    try:
        files0 = server._source_watch_files(src)
        server._LAST_REFRESH_MTIME = server._watch_max_mtime(files0)
        server._LAST_WATCH_FILES = tuple(files0)
        check(server._logdir_changed(src) is False, "D26 基线后无变化 → False")
        os.remove(f2)   # 删文件: mtime 不增 → 旧版漏; 集合变 → 补丁触发 refresh
        check(server._logdir_changed(src) is True, "D26 删一 watch 文件 → 集合变 → refresh (删盲区补丁)")
        # 重建 f2 重对齐集合, 再测增文件 (删/增对称验证)
        with open(f2, "w") as fh: fh.write("{}\n")
        server._LAST_WATCH_FILES = tuple(server._source_watch_files(src))
        server._LAST_REFRESH_MTIME = server._watch_max_mtime(server._source_watch_files(src))
        check(server._logdir_changed(src) is False, "D26 重对齐基线后无变化 → False")
        f3 = os.path.join(tmp, "ccc.jsonl")
        with open(f3, "w") as fh: fh.write("{}\n")
        check(server._logdir_changed(src) is True, "D26 增一 watch 文件 → 集合变 → refresh")
        # _watch_data_age 活性信号 (前端 chip: age<300s→实时, >300s→静止; 无源文件→None)
        age = server._watch_data_age(src)
        check(age is not None and age >= 0, "D26 _watch_data_age 有文件 → 非负 (距最新更新秒数)")
        empty = tempfile.mkdtemp()
        check(server._watch_data_age("scan:" + empty) is None, "D26 _watch_data_age 无文件 → None")
    finally:
        server._LAST_REFRESH_MTIME, server._LAST_WATCH_FILES = sv_m, sv_w


def test_live_source_watch_inject_stale():
    """D29 (live: 源 watch/age 闭环): 把 D24(isLive 注入) + D26(_logdir_changed 删盲区) + 活性翻转
    三件事在【live: 源】(record.py JSONL) 上补齐 —— 此前 D24 e2e 只证 transcript→isLive=False、
    D26 删盲区只证 scan: 源; live: 源 (两层 <base>/<projectName>/<date>.jsonl 布局) 的 watch glob /
    isLive=True / dataAgeSeconds 新鲜↔静止翻转 从未端到端跑过 (真 hook 红线 → 离线造 live logdir 钉住).
    (1) _source_watch_files('live:BASE') 命中两层 record.py 布局 + 单层兜底 (旧版只 glob 两层漏单层, server.py:158-164);
    (2) e2e live: 源 /api/result.isLive==True (D24 只证 transcript→False, 此处补 live→True) + dataAgeSeconds 在场且新鲜;
    (3) os.utime 拨旧 600s → _watch_data_age>STALE_AFTER_S (前端 chip →⏳静止); 拨回 now → <STALE_AFTER_S (→●实时).
    钉的是: server 只发 dataAgeSeconds 标量, 阈值在前端 (app.js STALE_AFTER_S=300); server 端两态皆能产."""
    STALE_AFTER_S = 300   # 镜像 app.js:117 STALE_AFTER_S (前端 chip 实时/静止阈值; server 只发 dataAgeSeconds, 阈值在前端)
    sys.path.insert(0, os.path.join(HERE, "..", "dashboard"))
    import server
    tmp = tempfile.mkdtemp()
    proj = os.path.join(tmp, "demo-live-watch")          # record.py 两层布局: <base>/<projectName>/<date>.jsonl
    os.makedirs(proj, exist_ok=True)
    two = os.path.join(proj, "2026-06-21.jsonl")         # 两层 (record.py 真实落点)
    one = os.path.join(tmp, "2026-06-21.jsonl")          # 单层兜底 (旧版 _watch_jsonl_under 只 glob 两层会漏单层)
    rec = json.dumps({
        "schemaVersion": 1, "timestamp": "2026-06-21T08:00:00+08:00",
        "runId": "watch-sid", "projectName": "demo-live-watch", "sessionId": "watch-sid",
        "toolUseId": "call-watch-1",
        "caller": {"agentId": None, "agentType": None, "isRoot": True},
        "recordType": "SubagentCall", "subagentType": "general-purpose",
        "spawned": {"agentId": "a-watch-1", "agentType": "general-purpose"},
        "tokens": {"input": 100, "output": 50, "cacheCreation": 0, "cacheRead": 0, "total": 150},
        "durationMs": 1000, "resolvedModel": "glm-5.1", "success": True, "error": None,
    }, ensure_ascii=False)
    with open(two, "w") as f: f.write(rec + "\n")
    with open(one, "w") as f: f.write(rec + "\n")
    src = "live:" + tmp
    # --- (1) live watch glob: 两层 + 单层都命中 (_watch_jsonl_under 自适应 glob, server.py:158-164) ---
    files = server._source_watch_files(src)
    rp_files = {os.path.realpath(p) for p in files}
    check(os.path.realpath(two) in rp_files,
          "D29 live watch 命中两层 <base>/<proj>/<date>.jsonl (record.py 真实布局)")
    check(os.path.realpath(one) in rp_files,
          "D29 live watch 命中单层 <base>/<date>.jsonl (旧版只 glob 两层会漏)")
    # --- (2) e2e: live: 源 /api/result.isLive==True + dataAgeSeconds 在场且新鲜 ---
    port = _free_port()
    proc = _start(port, src)
    try:
        check(_wait_ready(port), "D29 server ready (live: 源, watch/inject e2e)")
        s, body = _get(port, "/api/result")
        check(s == 200, f"D29 live: 源 /api/result 200, got {s}")
        res = json.loads(body)
        check(res.get("isLive") is True,
              f"D29 live: 源 /api/result.isLive == True (D24 只证 transcript→False, 此处补 live→True), got {res.get('isLive')}")
        age0 = res.get("dataAgeSeconds")
        check(age0 is not None and 0 <= age0 < STALE_AFTER_S,
              f"D29 live: 源 dataAgeSeconds 新鲜 (<{STALE_AFTER_S}s → 前端 chip ●实时), got {age0}")
    finally:
        proc.terminate(); proc.wait()
    # --- (3) 活性翻转: 拨旧 mtime → 静止 (>STALE_AFTER_S); 拨回 now → 实时 (<STALE_AFTER_S) ---
    # server._watch_data_age 每调用现算 (time.time() - max_mtime), 不缓存 → os.utime 即时反映 (server.py:206-211)
    old_t = time.time() - 600
    os.utime(two, (old_t, old_t)); os.utime(one, (old_t, old_t))
    age_stale = server._watch_data_age(src)
    check(age_stale is not None and age_stale > STALE_AFTER_S,
          f"D29 live watch 拨旧 600s → _watch_data_age>{STALE_AFTER_S} (前端 chip → ⏳静止), got {age_stale}")
    now_t = time.time()
    os.utime(two, (now_t, now_t)); os.utime(one, (now_t, now_t))
    age_fresh = server._watch_data_age(src)
    check(age_fresh is not None and age_fresh < STALE_AFTER_S,
          f"D29 live watch 拨回 now → _watch_data_age<{STALE_AFTER_S} (前端 chip → ●实时), got {age_fresh}")


def test_live_source_logdir_delete_blindspot():
    """D30 (live: 源 删盲区): D26 把 _logdir_changed 删盲区 (2026-06-21 补丁: 并比文件集) 证在 scan: 源;
    此处在【live: 源】(两层 watch) 上复证 —— 删 watch 文件 → 集合变 → refresh (单比 mtime 旧版漏删);
    增文件 → 集合变 → refresh. live 与 scan 共用 _watch_jsonl_under, 但 live 基 = record.py logdir、
    两层布局, 补证该路径删盲区同样生效 (此前仅 scan 证过)."""
    sys.path.insert(0, os.path.join(HERE, "..", "dashboard"))
    import server
    tmp = tempfile.mkdtemp()
    proj = os.path.join(tmp, "demo-del")                 # 两层布局 (与 record.py 一致)
    os.makedirs(proj, exist_ok=True)
    f1 = os.path.join(proj, "2026-06-20.jsonl")
    f2 = os.path.join(proj, "2026-06-21.jsonl")
    with open(f1, "w") as fh: fh.write("{}\n")
    with open(f2, "w") as fh: fh.write("{}\n")
    src = "live:" + tmp
    sv_m, sv_w = server._LAST_REFRESH_MTIME, server._LAST_WATCH_FILES   # 存档全局基线 (in-process 共享, finally 还原)
    try:
        files0 = server._source_watch_files(src)
        check(len(files0) == 2, f"D30 live watch 基线 2 文件 (两层), got {len(files0)}")
        server._LAST_REFRESH_MTIME = server._watch_max_mtime(files0)
        server._LAST_WATCH_FILES = tuple(files0)
        check(server._logdir_changed(src) is False, "D30 live: 基线后无变化 → False")
        os.remove(f2)                                     # 删: mtime 不增 → 旧版漏; 集合变 → 补丁触发 refresh
        check(server._logdir_changed(src) is True, "D30 live: 删一 watch 文件 → 集合变 → refresh (删盲区, live 路径)")
        with open(f2, "w") as fh: fh.write("{}\n")        # 重建重对齐集合, 再测增文件 (删/增对称)
        server._LAST_WATCH_FILES = tuple(server._source_watch_files(src))
        server._LAST_REFRESH_MTIME = server._watch_max_mtime(server._source_watch_files(src))
        check(server._logdir_changed(src) is False, "D30 live: 重对齐基线后无变化 → False")
        f3 = os.path.join(proj, "2026-06-22.jsonl")
        with open(f3, "w") as fh: fh.write("{}\n")
        check(server._logdir_changed(src) is True, "D30 live: 增一 watch 文件 → 集合变 → refresh")
    finally:
        server._LAST_REFRESH_MTIME, server._LAST_WATCH_FILES = sv_m, sv_w


def test_ctx_limit_errors_reader():
    """D25: count_ctx_limit_errors 爆掉事件检测 (§8.3 💥 状态 glyph 数据层; 2026-06-19 实证锁定信号).
    真信号 = type=assistant 顶层 text 块, strip 后以 'API Error' 起头 且含 'context window limit'.
    防 echo 假阳性: user/system 行、或 assistant 内非起头引用 → 不计 (实测 9aa81da2 grep 该串=4 但真爆=1,
    00cab3c5 grep=1 但真爆=0 —— 多余全 echo; 记忆原写的 isApiErrorMessage 信号不存在, 已更正)."""
    from transcript_adapter import count_ctx_limit_errors
    ERR = "API Error: The model has reached its context window limit."
    tmp = tempfile.mkdtemp()
    def _write(name, objs):
        p = os.path.join(tmp, name)
        with open(p, "w") as f:
            f.write("\n".join(json.dumps(o) for o in objs) + "\n")
        return p
    # 1. 真爆: assistant 顶层 text = 'API Error: ... context window limit.'
    real = _write("real.jsonl", [
        {"type": "assistant", "message": {"role": "assistant",
            "content": [{"type": "text", "text": ERR}]}},
    ])
    r = count_ctx_limit_errors(real)
    check(r["count"] == 1, f"D25 真爆 assistant 顶层 API Error → count=1, got {r['count']}")
    check(r["sample"] == ERR, f"D25 sample 取首个错误文本, got {r['sample']!r}")
    # 2. echo: 同串在 user 行 → 不计 (非 assistant)
    echo_user = _write("echo_user.jsonl", [
        {"type": "user", "message": {"role": "user",
            "content": [{"type": "text", "text": ERR}]}},
    ])
    check(count_ctx_limit_errors(echo_user)["count"] == 0, "D25 user 行 echo → count=0 (限 assistant)")
    # 3. echo: 同串在 system 行 → 不计
    echo_sys = _write("echo_sys.jsonl", [{"type": "system", "content": ERR}])
    check(count_ctx_limit_errors(echo_sys)["count"] == 0, "D25 system 行 echo → count=0")
    # 4. assistant 内非起头引用 (讨论错误, 不以 'API Error' 起头) → 不计
    discuss = _write("discuss.jsonl", [
        {"type": "assistant", "message": {"role": "assistant",
            "content": [{"type": "text", "text": "之前撞上 " + ERR + " 了"}]}},
    ])
    check(count_ctx_limit_errors(discuss)["count"] == 0, "D25 assistant 非起头引用 → count=0 (须 startswith 'API Error')")
    # 5. 干净 transcript → count=0
    clean = _write("clean.jsonl", [
        {"type": "assistant", "message": {"role": "assistant", "usage": {"input_tokens": 1000},
            "content": [{"type": "text", "text": "all good"}]}},
    ])
    check(count_ctx_limit_errors(clean)["count"] == 0, "D25 干净 transcript → count=0")
    # 6. bulletproof: 不存在路径 → count=0
    check(count_ctx_limit_errors(os.path.join(tmp, "nope.jsonl"))["count"] == 0, "D25 不存在路径 → count=0 (bulletproof)")


def test_ctx_limit_errors_e2e_and_frontend():
    """D26: 爆掉标记全链路 (reader→analyze→/api/result, mode_a transcript 路径) + app.js 三态契约.
    e2e: transcript 源带真爆 turn → perSession[0].ctxLimitErrors.count==1 (与 ctxPeak 并存, 复刻 9aa81da2 形态).
    frontend: ctxCell(peak,ctxErr) 三态 💥>⚠>正常; .ctx-blown/.ctx-fill-blown 红; footer 💥 legend."""
    sid = "aaaabbbb-1111-2222-3333-444455556666"
    root_path = os.path.join(tempfile.mkdtemp(), sid + ".jsonl")
    ERR = "API Error: The model has reached its context window limit."
    lines = [
        # usage turn → ctxPeak 9000 (root 主线; 真实 9aa81da2 形态: peak 与爆掉并存)
        json.dumps({"type": "assistant", "timestamp": "2026-06-19T10:00:00+08:00",
                    "message": {"role": "assistant",
                                "usage": {"input_tokens": 3000, "cache_creation_input_tokens": 0,
                                          "cache_read_input_tokens": 6000, "output_tokens": 100}}}),
        # spawn turn → perSession 行 (sessionId=sid 命中 _root_sid 归属; mirror D22)
        json.dumps({"timestamp": "2026-06-19T10:01:00+08:00", "sessionId": sid, "isSidechain": False,
                    "type": "assistant", "uuid": "u-1", "message": {"role": "assistant"},
                    "toolUseResult": {"status": "completed", "agentId": "agent-1", "agentType": "Explore",
                                      "totalDurationMs": 5000, "totalTokens": 5050,
                                      "usage": {"input_tokens": 500, "output_tokens": 50,
                                                "cache_creation_input_tokens": 0,
                                                "cache_read_input_tokens": 4500}}}),
        # 真爆 turn → ctxLimitErrors.count=1
        json.dumps({"type": "assistant", "timestamp": "2026-06-19T10:02:00+08:00",
                    "message": {"role": "assistant",
                                "content": [{"type": "text", "text": ERR}]}}),
    ]
    with open(root_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    port = _free_port()
    proc = _start(port, f"transcript:{root_path}")
    try:
        check(_wait_ready(port), "D26 server ready (transcript w/ blowup)")
        s, body = _get(port, "/api/result")
        check(s == 200, f"D26 /api/result 200, got {s}")
        res = json.loads(body)
        r = res.get("result", res)
        ps = r.get("perSession") or []
        check(len(ps) >= 1, f"D26 perSession 非空, got {len(ps)}")
        ce = ps[0].get("ctxLimitErrors") or {}
        check(ce.get("count") == 1, f"D26 perSession[0].ctxLimitErrors.count==1 (mode_a surface 爆掉事件), got {ce.get('count')}")
        check("context window limit" in (ce.get("sample") or ""), f"D26 sample 含 'context window limit', got {ce.get('sample')!r}")
    finally:
        proc.terminate(); proc.wait()
    # --- frontend 三态契约 (text-anchor) ---
    appjs = open(os.path.join(HERE, "..", "dashboard", "static", "app.js")).read()
    css = open(os.path.join(HERE, "..", "dashboard", "static", "style.css")).read()
    check("ctxCell(peak, ctxErr)" in appjs, "D26 app.js ctxCell 接 ctxErr (三态入口)")
    check("ctxErr.count > 0" in appjs, "D26 app.js blown 判定 ctxErr.count>0")
    check("💥" in appjs, "D26 app.js 💥 glyph (爆掉态)")
    check("ctx-blown" in appjs and "ctx-fill-blown" in appjs, "D26 app.js 引用 .ctx-blown / .ctx-fill-blown class")
    check(".ctx-blown" in css and ".ctx-fill-blown" in css, "D26 style.css 有 .ctx-blown / .ctx-fill-blown (红)")
    check("压缩失败爆掉" in appjs, "D26 app.js footer 💥 legend 文案 '压缩失败爆掉'")


def test_fleet_sort_and_footer():
    """D27: fleet 列头排序 + footer 友好化 (§8.3 总览表).
    排序: 默认 total desc; <th data-col> 可点切列/同列切换升降序; live-tail 2s 重渲染保留选中列 (不跳回 total).
    footer: 'fleet 合计' → '合计'; ctx peak 列合计行留空 — (求和无意义/MAX 混 sum 行语义/hero 已示 fleet-max/dur 同理留空); 删死码 maxCtxPeak."""
    appjs = open(os.path.join(HERE, "..", "dashboard", "static", "app.js")).read()
    html = open(os.path.join(HERE, "..", "dashboard", "static", "index.html")).read()
    # --- 排序基建 (app.js) ---
    check("let _sort = {" in appjs, "D27 app.js 有 _sort 状态")
    check('"total"' in appjs and '"desc"' in appjs, "D27 默认排序 total desc")
    check("FLEET_COLS" in appjs and "function sortRows" in appjs, "D27 app.js FLEET_COLS + sortRows")
    check("function initSort" in appjs and "function updateSortHeader" in appjs,
          "D27 app.js initSort + updateSortHeader")
    check("▲" in appjs and "▼" in appjs, "D27 app.js 升降序 glyph ▲/▼")
    check("th.dataset.col" in appjs, "D27 app.js 读 th.dataset.col 选列")
    check("_lastResult = result" in appjs, "D27 render 存 _lastResult (排序重渲染 + live-tail 续接)")
    check("render(_lastResult)" in appjs, "D27 点列头 → render(_lastResult) 重排 tbody")
    check("updateSortHeader()" in appjs, "D27 render 末调 updateSortHeader (列头 ▲/▼ 指示)")
    # --- index.html data-col ---
    for k in ("total", "session", "spawns", "dur", "cache", "fullin", "ctx", "ok"):
        check(f'data-col="{k}"' in html, f"D27 index.html <th data-col=\"{k}\">")
    check(html.count("data-col=") >= 8, f"D27 index.html 8 列全带 data-col, got {html.count('data-col=')}")
    # --- footer 友好化 ---
    check("fleet 合计" not in appjs, "D27 footer 去 'fleet 合计' 措辞")
    check('<td class="sess">合计</td>' in appjs, "D27 footer 只写 '合计'")
    check("maxCtxPeak" not in appjs, "D27 删死码 maxCtxPeak (footer ctx 不再取 max)")
    check('<td class="num"><span class="faint">—</span></td>' in appjs,
          "D27 footer ctx peak 列留空 — (ctxCell 同款空态; 求和无意义)")


def test_cache_context_union_count():
    """D28: cache/context 两面板 session 数目一致 (§8.3 hero 双面板).
    根因 (数据模型, 非 UI bug): totalTokens=subagent 用量 (grand_total of Events, §7) 与 ctxPeak=root
    主线 context (root_context_samples, §8.3) 是两条不同通道; 各面板各取一通道过滤 → 计数发散
    (实证: totalTokens>0=13, ctxPeak>0=16, 有 session ctxPeak>0 但 totalTokens=0).
    修法: 两面板共享活跃集 = 并集(totalTokens>0 ∪ ctxPeak>0); 各自口径无数据的 session 显 — (不伪造,
    计数仍一致). 一致性按构造保证: 两面板迭代同一 activeRows (等长 L); 每个 activeRow 在各面板恰产 1 行
    (含 — 行); spotlight 切片 (top4…N more 末2) 用同一基底长度 → "…N more" gap 与可见行数两面板恒等."""
    appjs = open(os.path.join(HERE, "..", "dashboard", "static", "app.js")).read()
    css = open(os.path.join(HERE, "..", "dashboard", "static", "style.css")).read()
    # --- 旧发散过滤已删 (两面板不再各自单通道过滤) ---
    check(".filter(r => (r.totalTokens || 0) > 0)" not in appjs,
          "D28 cache 面板不再单取 totalTokens>0 (改走 activeRows 并集)")
    check(".filter(r => (r.ctxPeak || 0) > 0)" not in appjs,
          "D28 context 面板不再单取 ctxPeak>0 (改走 activeRows 并集)")
    # --- 共享活跃集 = 并集 ---
    check("(r.totalTokens || 0) > 0 || (r.ctxPeak || 0) > 0" in appjs,
          "D28 并集谓词 totalTokens>0 ∪ ctxPeak>0")
    check("const activeRows = " in appjs, "D28 activeRows 共享活跃集定义")
    check("withHit = activeRows" in appjs, "D28 cache 面板 withHit 走 activeRows")
    check("ctxRows = activeRows" in appjs, "D28 context 面板 ctxRows 走 activeRows")
    # --- cache 面板 — 行: 统一计费口径后语义变了 (2026-06-19) ---
    # 旧逻辑: totalTokens<=0 (纯 root session, 无 subagent cache 数据) → 强制 —. 现已撤 — 纯 root session
    # 经合并 rootUsage 有真实命中率, 不再显 —. 旧守卫 (r.totalTokens||0)<=0 不应在 appjs.
    check("(r.totalTokens || 0) <= 0" not in appjs,
          "D28 cache 撤掉旧 totalTokens<=0 强制 — 守卫 (纯 root session 现有真实命中率)")
    # 新守卫: hit==null — 仅当合并计费三桶全 0 (空壳/纯 skill/output-only-subagent 边缘) 才显 —.
    check("hit == null" in appjs, "D28 cache — 分支守卫改 hit==null (分母 0, 非纯 root)")
    check("无计费 token (空壳/纯 skill)" in appjs, "D28 cache — 行 title 改 '无计费 token (空壳/纯 skill)'")
    # --- context 面板: 活跃但 root 主线无抽样 → — (替代误导的 2% 微条 + 0) ---
    check("peak <= 0" in appjs, "D28 context 分支守卫 peak<=0")
    check("无 root 主线 ctx 抽样" in appjs, "D28 context — 行 title (token 全在 subagent)")
    # --- fleet ctx 峰值只取真峰 (全 — 时不伪造) ---
    check("_ctxPeaks" in appjs and ".filter(p => p > 0)" in appjs,
          "D28 fleetCtxPeak 排除 0 峰 (全 — → null, 非伪造 max)")
    # --- ctxBody 改挂 activeRows (有活跃集就画 dist, 即使峰全 0 也显 — 行保计数) ---
    check("const ctxBody = activeRows.length" in appjs,
          "D28 ctxBody 以 activeRows 长度为闸 (dist 常显, 与 cache 面板计数对齐)")
    check("无此面板口径数据" in appjs, "D28 context legend 补 — 说明")
    # --- — 行淡化样式 ---
    check(".dist-row.is-na" in css, "D28 style.css .is-na 淡化 (— 行不参与色阶)")


def test_dist_row_name_hover_fullname():
    """D29: hero 双面板 dist-row hover 区分 session (§8.3) —— tooltip 补充 session 唯一标识, 非替换原提示.
    背景: dist-row 显示 project 名 (去 -home- 前缀); 同 project 多 session 显示同名, 且 project 全名也相同
    (实证: -home-qwren-demo-project 下 5 session 全显 'demo-project') → 单补 project 全名无法区分.
    修法: sessPrefix(r) = project全名 · sid<8> · 时长 · spawns (sid 唯一区分; 时长/spawns 人可辨), 并入 row 级
    title 单 tooltip; 原 hover 提示 (点进 session 视图 / 爆掉 / 纯 root 主线…) 全保留; .name span 不独立挂 title (HTML
    子元素 title 覆盖父元素 = 顶掉原提示 = 替换, 非补充)."""
    appjs = open(os.path.join(HERE, "..", "dashboard", "static", "app.js")).read()
    # --- 区分前缀 helper (project 全名 + sid + 时长 + spawns) ---
    check("function sessPrefix" in appjs, "D29 sessPrefix helper 定义")
    check("function projNameRaw" in appjs, "D29 projNameRaw helper (原始 project 名, 供拼接)")
    check('" · sid " + sid' in appjs or ('" · sid "' in appjs and '.slice(0, 8)' in appjs),
          "D29 sessPrefix 含短 sid (前 8 字符, 唯一区分同 project 多 session)")
    check("fmtDur(r.durationS)" in appjs, "D29 sessPrefix 含时长 (人可辨区分信号)")
    check(appjs.count("sessPrefix(r)") >= 4, f"D29 4 行 row title 都走 sessPrefix, got {appjs.count('sessPrefix(r)')}")
    # --- 原提示保留 + name span 不独立挂 title ---
    check("function nameCell" not in appjs, "D29 旧 nameCell 已撤 (它给 name span 独立 title 会覆盖原 row 提示)")
    check('<span class="name" title=' not in appjs, "D29 name span 不独立挂 title (避免顶掉 row 原提示)")
    check("点进 session 编排视图" in appjs, "D29 cache 正常行/drill 原提示保留")
    check("无计费 token (空壳/纯 skill)" in appjs, "D29 cache — 行 title (统一口径后改名, sessPrefix 仍拼)")
    check("无 root 主线 ctx 抽样" in appjs, "D29 context — 行原提示保留")
    check(appjs.count('class="name">…') == 3, f"D29 gap 行 …N more (hero cache+ctx 各 1 + session cache 书挡 1 = 3), got {appjs.count('class=\"name\">…')}")


def test_cache_hit_unified_billing_caliber():
    """D30: cache 命中率统一计费口径 (2026-06-19) — 合并 subagent (grandTotal) + root 主线 (rootUsage).
    用户原则: token 按计费规则算, 不被重复算钱的全算上; cache 命中率 = 合并 cacheRead / (cacheRead + input + cc),
    output 永不进缓存. 各 turn cacheRead 是独立真实计费事件, 累加非重复. 纯 root session (无 subagent) 现也
    有真实命中率 (经 rootUsage), 不再显 —. fleet 头条同样合并跨 session rootUsage + grandTotal."""
    appjs = open(os.path.join(HERE, "..", "dashboard", "static", "app.js")).read()
    # --- 合并计费三桶 helper (grandTotal + rootUsage 逐桶相加) ---
    check("const billable = (r) =>" in appjs, "D30 billable helper 定义 (合并三桶)")
    check("r.grandTotal || {}" in appjs and "r.rootUsage || {}" in appjs,
          "D30 billable 合并 grandTotal + rootUsage 两源")
    check("(g.cacheRead || 0) + (ru.cacheRead || 0)" in appjs,
          "D30 合并公式: 逐桶 grandTotal + rootUsage 相加 (各源独立计费, 累加非重复)")
    # --- 命中率 = 合并 cacheRead / (合并 cacheRead + input + cc); output 不进 ---
    check("const hitBillable = (r) =>" in appjs, "D30 hitBillable helper 定义")
    check("b.cacheRead + b.input + b.cacheCreation" in appjs,
          "D30 命中率分母 = cacheRead + input + cc (output 永不进缓存, 计费口径)")
    # --- sessHit 走合并口径 (非旧 hitInputSide(grandTotal) sub-only) ---
    check("const sessHit = (r) => hitBillable(r)" in appjs,
          "D30 sessHit 委托 hitBillable (统一口径; 取代旧 sub-only hitInputSide)")
    # --- 纯 root session 有真实命中率: 旧 totalTokens<=0 强制 — 守卫已撤 (D28 也断) ---
    check("(r.totalTokens || 0) <= 0" not in appjs,
          "D30 纯 root session 不再被 totalTokens<=0 强制显 — (合并 rootUsage 有真实命中率)")
    # --- fleet 头条合并跨 session rootUsage + grandTotal ---
    check("const fleetRoot" in appjs, "D30 fleet 头条 fleetRoot (跨 session rootUsage 累加)")
    check("_fleetCR = (gt.cacheRead || 0) + fleetRoot.cacheRead" in appjs,
          "D30 _fleetCR = sub(grandTotal.cacheRead) + root(fleetRoot.cacheRead)")
    check("全局 cache hit（计费口径）" in appjs,
          "D30 metric 标注 '计费口径' (root主线+subagent 语义下沉到 hint, 避免与下方重复)")
    check("各源独立计费, 累加非重复" in appjs, "D30 hint 注明计费语义 (累加非重复)")
    # --- tierOf 用合并 token 量判 empty (纯 root totalTokens=0 但 billableTotal>0 不误判 empty 灰) ---
    check("tierOf(hit, billableTotal(r))" in appjs,
          "D30 tierOf 用 billableTotal (合并量) 判 empty — 纯 root 不误判 empty")


def test_root_usage_transcript_sum():
    """D31: perSession[].rootUsage = root 主线逐 turn 真实计费 sum (transcript_adapter root_context_samples;
    2026-06-19). 锁死 sum 而非 peak 单点: 两 root turn 的 input/cc/cr 各自累加. 纯 root session (0 subagent)
    在 scan 模式仍进 perSession (0-Agent 合法零-spawn mini-result), grandTotal 全 0 但 rootUsage>0 → 经合并
    口径有真实命中率 (app.js sessHit). 期望: turn0 (in3000/cc0/cr6000, ctx9000) + turn1 (in1000/cc500/cr2000,
    ctx3500) → sum{in4000/cc500/cr8000}; peak=9000 (单 turn, ≠sum.cacheRead 8000)."""
    import json as _json
    tmp = tempfile.mkdtemp()
    proj = os.path.join(tmp, "-home-d31-pure-root")
    sid = "a1b2c3d4-1111-2222-3333-444455556666"   # UUID 形 (discover_root_transcripts 过滤规则)
    os.makedirs(proj, exist_ok=True)
    root_path = os.path.join(proj, sid + ".jsonl")
    lines = [
        # root turn 0: input 3000 + cacheRead 6000 → ctx 9000 (将是 peak)
        _json.dumps({"type": "assistant", "timestamp": "2026-06-19T10:00:00+08:00",
                     "message": {"role": "assistant",
                                 "usage": {"input_tokens": 3000, "cache_creation_input_tokens": 0,
                                           "cache_read_input_tokens": 6000, "output_tokens": 100}}}),
        # root turn 1: input 1000 + cc 500 + cacheRead 2000 → ctx 3500
        _json.dumps({"type": "assistant", "timestamp": "2026-06-19T10:01:00+08:00",
                     "message": {"role": "assistant",
                                 "usage": {"input_tokens": 1000, "cache_creation_input_tokens": 500,
                                           "cache_read_input_tokens": 2000, "output_tokens": 50}}}),
    ]
    # 无 subagent 行 → 纯 root session (scan 模式 0-Agent 合法, 仍进 perSession)
    with open(root_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    port = _free_port()
    proc = _start(port, f"scan:{tmp}")
    try:
        check(_wait_ready(port), "D31 server ready (scan source)")
        s, body = _get(port, "/api/result")
        check(s == 200, f"D31 /api/result 200, got {s}")
        res = _json.loads(body)
        ps = res.get("perSession") or []
        row = next((r for r in ps if r.get("sid") == sid), None)
        check(row is not None, f"D31 scan 含纯 root session (0 subagent) perSession 行, got {len(ps)} rows")
        ru = row.get("rootUsage") or {}
        # --- sum 而非 peak: 两 turn 各自累加 ---
        check(ru.get("input") == 4000, f"D31 rootUsage.input == 4000 (3000+1000 sum), got {ru.get('input')}")
        check(ru.get("cacheCreation") == 500, f"D31 rootUsage.cacheCreation == 500 (0+500 sum), got {ru.get('cacheCreation')}")
        check(ru.get("cacheRead") == 8000, f"D31 rootUsage.cacheRead == 8000 (6000+2000 sum, 非 peak 单点 6000), got {ru.get('cacheRead')}")
        # --- peak 是单 turn 峰值 (9000), 与 sum 三桶不同量 → 证 sum 非 peak 派生 ---
        check(row.get("ctxPeak") == 9000, f"D31 ctxPeak == 9000 (turn0 单点峰值, 非 sum), got {row.get('ctxPeak')}")
        check(row.get("ctxPeak") != ru.get("cacheRead"), "D31 peak(9000) ≠ rootUsage.cacheRead(8000) — sum 与 peak 不同量")
        # --- 纯 root: grandTotal/totalTokens 全 0, 但 rootUsage>0 → 合并口径有真实命中率 ---
        gt = row.get("grandTotal") or {}
        check((gt.get("total") or 0) == 0 and (row.get("totalTokens") or 0) == 0,
              f"D31 纯 root session grandTotal/totalTokens 全 0 (无 subagent), got total={gt.get('total')} tt={row.get('totalTokens')}")
        check((ru.get("cacheRead") or 0) + (ru.get("input") or 0) + (ru.get("cacheCreation") or 0) > 0,
              "D31 纯 root session rootUsage>0 → app.js sessHit 经合并口径有真实命中率 (不再显 —)")
    finally:
        proc.terminate(); proc.wait()


def test_root_usage_dedup_multiline_message():
    """D32: root_context_samples 按 message id 去重 (2026-06-19 root 双计 bug 回归守卫).
    CC transcript 把一条 assistant message 按内容块 (thinking/text/tool_use) 拆成多行, 每行各挂 message.usage;
    中间块 stop_reason=None 带占位 usage (全量 input、cr/cc=0), 仅终态行 (带 stop_reason) 是真计费.
    旧逐行求和把一条 message 算 N 遍 → input 虚胖 (实测 4a4d9e01: 50×). 本测构造一条 message 拆 3 行
    (同 id, 仅末行 stop_reason) + 一条独立单行 message, 断 sum.input 按去重后算 (3 行 msg 只留终态行 1 次),
    非 3 行占位 input 累加."""
    import json as _json
    tmp = tempfile.mkdtemp()
    proj = os.path.join(tmp, "-home-d32-dedup")
    sid = "d3d3d3d3-2222-3333-4444-555566667777"   # UUID 形 (discover_root_transcripts 过滤规则)
    os.makedirs(proj, exist_ok=True)
    root_path = os.path.join(proj, sid + ".jsonl")
    lines = [
        # === message A: 一条 message 拆 3 行 (同 id), 仅末行带 stop_reason ===
        # A1: 中间块占位 usage (全量 input 100000, cr/cc=0, 无 stop_reason) — 旧逐行求和会算进去
        _json.dumps({"type": "assistant", "timestamp": "2026-06-19T10:00:00+08:00",
                     "message": {"role": "assistant", "id": "msg_aaa",
                                 "usage": {"input_tokens": 100000, "cache_creation_input_tokens": 0,
                                           "cache_read_input_tokens": 0, "output_tokens": 10}}}),
        # A2: 又一中间块占位 (input 100000, cr/cc=0, 无 stop_reason)
        _json.dumps({"type": "assistant", "timestamp": "2026-06-19T10:00:01+08:00",
                     "message": {"role": "assistant", "id": "msg_aaa",
                                 "usage": {"input_tokens": 100000, "cache_creation_input_tokens": 0,
                                           "cache_read_input_tokens": 0, "output_tokens": 10}}}),
        # A3: 终态行 (带 stop_reason) — 真计费: input 2000 + cacheRead 5000 → ctx 7000
        _json.dumps({"type": "assistant", "timestamp": "2026-06-19T10:00:02+08:00",
                     "message": {"role": "assistant", "id": "msg_aaa", "stop_reason": "end_turn",
                                 "usage": {"input_tokens": 2000, "cache_creation_input_tokens": 0,
                                           "cache_read_input_tokens": 5000, "output_tokens": 50}}}),
        # === message B: 独立单行 (不同 id, 带 stop_reason): input 1000 + cc 100 + cacheRead 2000 ===
        _json.dumps({"type": "assistant", "timestamp": "2026-06-19T10:01:00+08:00",
                     "message": {"role": "assistant", "id": "msg_bbb", "stop_reason": "end_turn",
                                 "usage": {"input_tokens": 1000, "cache_creation_input_tokens": 100,
                                           "cache_read_input_tokens": 2000, "output_tokens": 30}}}),
    ]
    with open(root_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    port = _free_port()
    proc = _start(port, f"scan:{tmp}")
    try:
        check(_wait_ready(port), "D32 server ready (scan source)")
        s, body = _get(port, "/api/result")
        check(s == 200, f"D32 /api/result 200, got {s}")
        res = _json.loads(body)
        ps = res.get("perSession") or []
        row = next((r for r in ps if r.get("sid") == sid), None)
        check(row is not None, f"D32 scan 含 root session perSession 行, got {len(ps)} rows")
        ru = row.get("rootUsage") or {}
        # === 核心去重断言: msg A 拆 3 行 (2 行占位 input=100000 + 1 行终态 input=2000), 只留终态 2000 一次 ===
        # 旧逐行求和: input = 100000+100000+2000+1000 = 203000 (虚胖 ~68×). 去重后: 2000+1000 = 3000.
        check(ru.get("input") == 3000, f"D32 rootUsage.input == 3000 (msg A 去重只留终态 2000 + msg B 1000; "
              f"旧逐行求和会得 203000), got {ru.get('input')}")
        # cacheRead 不受去重影响 (占位行 cr=0), 但仍验证: A3 终态 5000 + B 2000 = 7000
        check(ru.get("cacheRead") == 7000, f"D32 rootUsage.cacheRead == 7000 (A3 终态 5000 + B 2000), got {ru.get('cacheRead')}")
        # cacheCreation: A3 终态 0 + B 100 = 100
        check(ru.get("cacheCreation") == 100, f"D32 rootUsage.cacheCreation == 100 (A3 终态 0 + B 100), got {ru.get('cacheCreation')}")
        # ctxPeak = 单 message 峰值 = A3 终态 ctx 7000 (≠ sum.cacheRead 7000 同值但量纲不同; 取 max over 去重后 messages)
        check(row.get("ctxPeak") == 7000, f"D32 ctxPeak == 7000 (msg A 终态行峰值, 非 3 行占位累加), got {row.get('ctxPeak')}")
    finally:
        proc.terminate(); proc.wait()


def test_fleet_table_merged_caliber():
    """D33: fleet 总览表 total/cache 单元格走合并计费口径 (2026-06-19 用户报障 #3 回归守卫).
    用户: '总览面板里数量也都是错的, ctx峰值有, 结果 total 里显示 0; cache命中率也要和面板里保持一致'.
    根因: 旧表 total 单元格用 r.totalTokens (sub-only, 纯 root session=0); cache 单元格用 r.cacheReadPct (sub-only).
    修: total→billableTotal(r) (合并 root+sub), cache→sessHit(r) (== hero/cache 面板同 helper). 本测锁源码, 不跑 render()."""
    appjs = open(os.path.join(HERE, "..", "dashboard", "static", "app.js")).read()
    # --- FLEET_COLS total/cache 列定义走合并口径 ---
    check("get: r => billableTotal(r)" in appjs,
          "D33 FLEET_COLS total 列 = billableTotal(r) (合并 root+sub, 纯 root 不再显 0)")
    check("sessHit(r)" in appjs and "h != null ? h * 100 : -1" in appjs,
          "D33 FLEET_COLS cache 列 = sessHit(r)*100 (合并命中率, 与 cache 面板同 helper)")
    # --- fleet 行渲染: total/cache 单元格的基底是合并三桶 ---
    check("const b = billable(r)" in appjs, "D33 fleet 行取 billable(r) (合并三桶)")
    check("const bt = b.cacheRead + b.input + b.cacheCreation" in appjs,
          "D33 fleet 行 bt = 合并三桶和 (total 单元格基底, 非 r.totalTokens)")
    check("const hit = sessHit(r)" in appjs,
          "D33 fleet 行 hit = sessHit(r) (cache 单元格基底, 非 r.cacheReadPct)")
    # --- 单元格渲染: total 显 fmt(bt), cache 显 (hit*100)% (== hero/cache 面板同口径数字) ---
    check("${fmt(bt)}" in appjs, "D33 total 单元格渲染 fmt(bt) (合并量, 纯 root 有真实 total 非 0)")
    check("${(hit * 100).toFixed(1)}%" in appjs, "D33 cache 单元格渲染 (hit*100)% (合并命中率, 与 cache 面板一致)")
    # --- NEGATIVE: 旧 sub-only cacheReadPct 字段已从 app.js 全清 (cache 命中率跨面板一致的前提) ---
    check("cacheReadPct" not in appjs, "D33 旧 sub-only cacheReadPct 字段已清 (cache 命中率统一走 sessHit)")


def test_session_drill_merged_caliber():
    """D34: session 钻取页 (session 视图 showSession) total/cache hit 走合并口径 (2026-06-19 用户报障: 钻取页与主面板不一致).
    用户: '从 session 行点进去的 session 页面, 显示的 total/cache hit 等数字也和主面板不一致'.
    根因: showSession 头 chip 用 gt.total (sub-only grandTotal) + hitInputSide(gt) (sub-only) → 纯 root session (gt 全 0)
    显 total 0 / cache hit —, 与 fleet 表 (合并口径) 矛盾. 修: 合并 d.grandTotal + d.rootContext.sum →
    billableTotal/sessHit (== fleet 表). 注意 spawn 比对基线 (_sessionCtx.sessionHit) 仍 sub-only — spawn 是 subagent,
    其 hit 只该和 subagent 均值比 (relabel 'session 均值' → 'subagent 均值' 防与合并头 chip 混淆)."""
    appjs = open(os.path.join(HERE, "..", "dashboard", "static", "app.js")).read()
    # --- showSession 构合并 row (sub grandTotal + root 主线 rootContext.sum) ---
    check("grandTotal: gt, rootUsage: (d.rootContext || {}).sum || {}" in appjs,
          "D34 showSession 合并 d.grandTotal + d.rootContext.sum 为 billable row")
    check("const sessTot = billableTotal(_sess)" in appjs,
          "D34 showSession sessTot = billableTotal (合并 session 计费总量)")
    check("const sessHitVal = sessHit(_sess)" in appjs,
          "D34 showSession sessHitVal = sessHit (合并 session 命中率)")
    # --- 头 chip 用合并量 (与 fleet 表/hero 一致) ---
    check("total <b>${fmt(sessTot)}</b>" in appjs,
          "D34 钻取头 total chip = fmt(sessTot) (合并, 纯 root 不显 0)")
    check("cache hit <b>${sessHitVal != null ? (sessHitVal * 100).toFixed(1)" in appjs,
          "D34 钻取头 cache hit chip = sessHitVal (合并, 与主面板一致)")
    # --- NEGATIVE: 旧 sub-only 头 chip 模板已撤 ---
    check("cache hit <b>${hitInputSide(gt)" not in appjs,
          "D34 旧 sub-only 钻取头 chip (hitInputSide(gt)) 已撤")
    # --- spawn 比对基线仍 sub-only (spawn 是 subagent), 但 relabel 防与合并头混淆 ---
    check("sessionHit: hitInputSide(gt)" in appjs,
          "D34 _sessionCtx.sessionHit 仍 sub-only (spawn 比对基线; spawn hit 只该和 subagent 均值比)")
    check("vs subagent ${sessHitPct}%" in appjs and "subagent 均值" in appjs,
          "D34 spawn 比对 relabel 'subagent 均值' (防与合并头 chip 混淆)")
    # --- by-skill session 列表 tail 也合并 (纯 root 不显 0 tok) ---
    check("fmt(billableTotal(r))} tok" in appjs,
          "D34 by-skill session tail = billableTotal(r) (合并, == fleet total 列)")


def _write_agent_transcript(path, turns):
    """turns: [(msg_id, stop_reason, usage_dict), ...] → assistant 行 (终态块求和去重基底).
    每条 distinct message.id + stop_reason set = 终态块 (真计费); 模拟 CC agent transcript."""
    lines = []
    for mid, stop, usage in turns:
        lines.append(json.dumps({"type": "assistant", "timestamp": "2026-06-18T10:00:00+08:00",
                    "message": {"role": "assistant", "id": mid, "stop_reason": stop, "model": "glm-5.1",
                                "usage": usage, "content": [{"type": "text", "text": "x"}]}}))
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def _root_spawn_line(**fields):
    """一行 root assistant 行, toolUseResult = fields (spawn 记录; comp/async 共用)."""
    return json.dumps({"type": "assistant", "timestamp": "2026-06-18T10:00:00+08:00",
            "message": {"role": "assistant"}, "toolUseResult": fields})


def test_spawn_agentfile_token_override():
    """D35: 有 agent-<id>.jsonl 的 spawn → token 用该文件终态累计 (message.id 去重), 覆盖 root 末轮值.
    实证根因 (session 7d4eb5c6): root toolUseResult.usage 只携末轮 API usage (cr 逐位 == agent 末轮),
    真实累计 11-31×; _build_record 改用 agent 文件终态累计为权威源."""
    from transcript_adapter import load_transcript
    from types import SimpleNamespace
    tmp = tempfile.mkdtemp()
    sid = "cccc1111-2222-3333-4444-555566667777"
    root = os.path.join(tmp, sid + ".jsonl")
    sub = os.path.join(tmp, sid, "subagents")
    os.makedirs(sub)
    # agent 文件: 2 条 distinct 终态 message, 各 cacheRead 3000 → 累计 6000 (root 末轮只 500)
    _write_agent_transcript(os.path.join(sub, "agent-comp1.jsonl"),
        [("m-comp-1", "tool_use", {"input_tokens": 1000, "cache_read_input_tokens": 3000,
                                    "cache_creation_input_tokens": 0, "output_tokens": 50}),
         ("m-comp-2", "end_turn", {"input_tokens": 1000, "cache_read_input_tokens": 3000,
                                    "cache_creation_input_tokens": 0, "output_tokens": 50})])
    with open(root, "w") as f:
        f.write(_root_spawn_line(status="completed", agentId="comp1", agentType="Explore",
                totalDurationMs=42000, totalTokens=999,
                usage={"input_tokens": 100, "cache_read_input_tokens": 500,
                       "cache_creation_input_tokens": 0, "output_tokens": 10},
                resolvedModel="glm-5.1") + "\n")
    recs, _, _ = load_transcript(SimpleNamespace(transcript=root, project="ptest"))
    sub_recs = [r for r in recs if r.get("spawned", {}).get("agentId") == "comp1"]
    check(len(sub_recs) == 1, "D35 命中 comp1 spawn record")
    tk = sub_recs[0]["tokens"]
    check(tk["cacheRead"] == 6000,
          f"D35 token 用 agent 文件终态累计 cacheRead=6000 (非 root 末轮 500), got {tk.get('cacheRead')}")
    check(tk["total"] == 8100,
          f"D35 total = 四桶累计 (1000+1000+3000+3000+50+50)=8100, got {tk.get('total')}")


def test_async_spawn_status_and_tokens():
    """D36: async_launched spawn — status 透传 + agent 文件补 token (root usage 恒 None) +
    .meta.json 补 agentType (root 缺, 否则误显 unknown) + success=False (非 completed, 非 failed)."""
    from transcript_adapter import load_transcript
    from types import SimpleNamespace
    tmp = tempfile.mkdtemp()
    sid = "dddd1111-2222-3333-4444-555566667777"
    root = os.path.join(tmp, sid + ".jsonl")
    sub = os.path.join(tmp, sid, "subagents")
    os.makedirs(sub)
    _write_agent_transcript(os.path.join(sub, "agent-async1.jsonl"),
        [("m-async-1", "end_turn", {"input_tokens": 1000, "cache_read_input_tokens": 8000,
                                     "cache_creation_input_tokens": 0, "output_tokens": 80})])
    with open(os.path.join(sub, "agent-async1.meta.json"), "w") as f:
        json.dump({"agentType": "demo-designer", "toolUseId": "tu-async-1",
                   "name": "designer", "description": "d"}, f)
    # root spawn: async_launched, 无 agentType, 无 usage (真实 async ack 形态)
    with open(root, "w") as f:
        f.write(_root_spawn_line(status="async_launched", agentId="async1",
                totalDurationMs=None) + "\n")
    recs, _, _ = load_transcript(SimpleNamespace(transcript=root, project="ptest"))
    sub_recs = [r for r in recs if r.get("spawned", {}).get("agentId") == "async1"]
    check(len(sub_recs) == 1, "D36 命中 async1 spawn record")
    r = sub_recs[0]
    check(r["status"] == "async_launched", f"D36 status 透传 = async_launched, got {r.get('status')}")
    check(r["subagentType"] == "demo-designer",
          f"D36 agentType 从 .meta.json 补 (非 unknown), got {r.get('subagentType')}")
    check(r["tokens"]["cacheRead"] == 8000,
          f"D36 token 从 agent 文件补 cacheRead=8000 (root usage None), got {r['tokens'].get('cacheRead')}")
    check(r["success"] is False, "D36 async → success=False (非 completed)")
    check(r.get("toolUseId") == "tu-async-1", f"D36 toolUseId 从 .meta.json 补, got {r.get('toolUseId')}")


def test_status_propagates_callchains():
    """D37: status 经 to_event → build_topology (dict 拷贝) 透传到 callChains 每节点 (前端健康信号分轨依据)."""
    from transcript_adapter import load_transcript
    from analyze import to_event, build_topology
    from types import SimpleNamespace
    tmp = tempfile.mkdtemp()
    sid = "eeee1111-2222-3333-4444-555566667777"
    root = os.path.join(tmp, sid + ".jsonl")
    sub = os.path.join(tmp, sid, "subagents")
    os.makedirs(sub)
    _write_agent_transcript(os.path.join(sub, "agent-a1.jsonl"),
        [("m-a1", "end_turn", {"input_tokens": 500, "cache_read_input_tokens": 2000,
                                "cache_creation_input_tokens": 0, "output_tokens": 20})])
    _write_agent_transcript(os.path.join(sub, "agent-a2.jsonl"),
        [("m-a2", "end_turn", {"input_tokens": 500, "cache_read_input_tokens": 2000,
                                "cache_creation_input_tokens": 0, "output_tokens": 20})])
    with open(root, "w") as f:
        f.write(_root_spawn_line(status="completed", agentId="a1", agentType="Explore",
                usage={"input_tokens": 100, "cache_read_input_tokens": 100,
                       "cache_creation_input_tokens": 0, "output_tokens": 10}) + "\n")
        f.write(_root_spawn_line(status="async_launched", agentId="a2") + "\n")
    recs, _, _ = load_transcript(SimpleNamespace(transcript=root, project="ptest"))
    evs = [to_event(r) for r in recs if r.get("recordType") == "SubagentCall"]
    check(all("status" in e for e in evs), "D37 to_event 每条带 status 字段")
    by_id = {e.get("spawnedAgentId"): e.get("status") for e in evs}
    check(by_id.get("a1") == "completed" and by_id.get("a2") == "async_launched",
          f"D37 status 正确分轨 (a1=completed, a2=async_launched), got {by_id}")
    topo = build_topology(evs)
    check(all("status" in n for n in topo), "D37 build_topology (callChains) 每节点带 status")
    topo_by = {n.get("spawnedAgentId"): n.get("status") for n in topo}
    check(topo_by.get("a2") == "async_launched", "D37 callChains 节点 a2 = async_launched (前端可分轨)")


def test_async_ui_section():
    """D38: app.js async 信号 —— 头部 异步/失败 chip (按 status 分轨, async≠failed) + 时间轴 async 启动竖线
    (运行时长未知) + async 折进 agents 面板 (agent-tag.async) 而非独列. (静态文本契约; 2026-06-19 重构)"""
    appjs = open(os.path.join(HERE, "..", "dashboard", "static", "app.js")).read()
    css = open(os.path.join(HERE, "..", "dashboard", "static", "style.css")).read()
    # 头部 chip: async≠failed 分轨 (status 既非 completed 也非 async_launched 才算 失败)
    check('"chip warn"' in appjs and "异步 <b>" in appjs, "D38 app.js 头部 异步 chip.warn (run_in_background, 非 failed)")
    check('class="chip err"' in appjs and "失败" in appjs, "D38 app.js 头部 真'失败' chip.err")
    check("异步后台" not in appjs, "D38 app.js 去 '异步后台' 旧文案 (改 '异步' chip)")
    # 时间轴: ganttSegs 排除 async (0 宽 sliver); asyncSegs 画竖线 marker (运行时长未知)
    check("ganttSegs" in appjs, "D38 app.js gantt 用 ganttSegs (排除 async 0 宽 sliver)")
    check('class="gantt-async' in appjs, "D38 app.js 时间轴 async 启动竖线 .gantt-async (可带 .multi 并发簇)")
    check("运行时长未知" in appjs, "D38 app.js async 竖线诚实标 '运行时长未知' (完成走 task-notification 不回写)")
    # async 折进 agents 面板 (消重), 非独列; 旧 .async-list/.async-row/.twin-row.async 已删
    check('class="async-list"' not in appjs and 'class="async-row"' not in appjs,
          "D38 app.js 删异步独列 (.async-list/.async-row) —— async 折进 agents 面板")
    check('"agent-tag async"' in appjs, "D38 app.js agents 面板 .agent-tag async 徽标")
    for sel in (".chip.warn", ".chip.err", ".async-tag", ".gantt-async"):
        check(sel in css, f"D38 style.css 含 {sel}")


def test_session_facets():
    """D44: showSession 四切面契约 (Request C) —— 时间轴 async 启动竖线 / cache 书挡 (镜像 hero) /
    agents 花名册面板 (sync+异步+失败 统一) / skill 切面 (SkillCall) / 调用拓扑缩进树; 全可点 → spawn 详情. (静态文本契约)"""
    appjs = open(os.path.join(HERE, "..", "dashboard", "static", "app.js")).read()
    css = open(os.path.join(HERE, "..", "dashboard", "static", "style.css")).read()

    # 1) 时间轴 async 启动竖线 (无宽度 marker, 运行时长未知): asyncSegs 子集; 轴范围 sync∪async 联合求
    check("asyncSegs" in appjs, "D44 asyncSegs (异步子集, 画竖线 marker)")
    check('class="gantt-async' in appjs and "data-agentid=" in appjs, "D44 .gantt-async 竖线可点 (data-agentid) → spawn 详情")
    # 时间轴标尺 (0→总时长) + 可读时长 (去 ms) + async 并发折线 ×N (同 turn 扇出重叠非丢失)
    check("function fmtDurMs" in appjs, "D44 fmtDurMs 助手 (ms → xh xm xs 可读; tooltip/标尺共用)")
    check('class="gantt-axis"' in appjs, "D44 时间轴标尺 .gantt-axis (0 → 中点 → 总时长)")
    check("asyncByMoment" in appjs, "D44 async 按 timestamp 折线 (asyncByMoment 并发分组)")
    check('class="gantt-async-n"' in appjs and "×${n}" in appjs, "D44 并发簇 ×N 胶囊 (同 turn N 个, 非丢失)")
    check('gantt-async${multi ? " multi" : ""}' in appjs, "D44 并发簇标 .multi class (胶囊替 ▲)")
    # 同名并发消歧 (用户: "同名的也不知道对应面板里哪个"): tooltip 给 roster 序号 #i (与 agents 行徽章对齐)
    # + 类型去重计数 (全同名 → N× Type; 混名 → N 个 (A×a, B×b)); data-agentids 供悬停高亮
    check("segs.indexOf(g)" in appjs and "idxStr" in appjs, "D44 async 簇 tooltip roster 序号 (segs.indexOf, 与 #i 对齐)")
    check("并发启动" in appjs and "×${c}" in appjs, "D44 类型去重计数 (全同名 N× / 混名 A×a,B×b)")
    check("data-agentids=" in appjs, "D44 段/竖线带 data-agentids (悬停高亮联动键, 并发簇多 id 全亮)")
    for sel in (".gantt-axis", ".gantt-async-n", ".gantt-async.multi::before"):
        check(sel in css, f"D44 style.css 含 {sel} (标尺/胶囊/并发簇)")

    # 2) cache 书挡 (镜像 hero app.js:180-215): 全 segs 按 hit 降序, ≤7 全显, >7 头4+…N more+末2
    check("cacheSorted" in appjs and "slice(0, 4)" in appjs and "slice(-2)" in appjs,
          "D44 cache 书挡排序 (hit 降序) + 头4/末2 切片")
    check('class="dist-row dist-more"' in appjs, "D44 …N more 书挡行 (镜像 hero .dist-more)")
    check("jumpToAgentsPanel" in appjs, "D44 …N more → jumpToAgentsPanel (滚闪 agents 面板)")
    check(bool(re.search(r'function jumpToAgentsPanel\(\)\s*\{', appjs)), "D44 jumpToAgentsPanel 定义存在")
    check("scrollIntoView" in appjs, "D44 jumpToAgentsPanel 用 scrollIntoView 滚到面板")

    # 3) agents 花名册面板: 全 spawn 统一一行 (sync+异步+失败), 状态徽标, 点行 → drillSpawn
    check('id="agents-panel"' in appjs, "D44 agents 面板 #agents-panel")
    check('class="agent-list"' in appjs, "D44 .agent-list 容器")
    for tag in ('"agent-tag done"', '"agent-tag async"', '"agent-tag fail"'):
        check(tag in appjs, f"D44 agents 状态徽标 {tag}")
    check(bool(re.search(r'agentListEl.*?addEventListener.*?drillSpawn', appjs, re.S)),
          "D44 agents 行点击委托 drillSpawn → spawn 详情")
    # 同名 agent 可定位: 每行 #i roster 徽章 + data-idx; timeline↔agents 双向 hover 高亮 (直接看到而非读序号)
    check('class="agent-idx"' in appjs and 'data-idx="${i}"' in appjs, "D44 agents 行 #i 徽章 + data-idx (roster 序号)")
    check(".agent-idx" in css, "D44 .agent-idx 徽章样式 (roster 序号)")
    check('"mouseover"' in appjs and '"mouseleave"' in appjs,
          "D44 timeline↔agents hover 事件 (mouseover/mouseleave 委托)")
    check('classList.toggle("hl"' in appjs, "D44 hover 高亮 .hl class toggle (双向联动)")
    for sel in (".agent-row.hl", ".gantt-seg.hl", ".gantt-async.hl"):
        check(sel in css, f"D44 style.css 含 {sel} (hover 高亮)")
    # #i 恒为时序序号 (用户: "按时序编个号"): segs 显式按 start 排序, 不依赖上游 callChains 顺序
    check("segs.sort(" in appjs and "a.start - b.start" in appjs,
          "D44 segs 显式时序排序 → #i 恒为时序序号 (同 timestamp 并发顺序任意)")
    # 并发簇点击行为 (用户: "同刻启动的不进详情, 先移到 agents 面板高亮处选一个"): multi → 滚闪 .sel/.flash, 不直接 drill
    check('classList.contains("multi")' in appjs and 'classList.add("sel")' in appjs,
          "D44 并发簇 (multi) 点击 → 滚 agents 面板 + .sel 锁定 (非直接 drillSpawn)")
    check('classList.add("flash")' in appjs and "agent-flash" in css,
          "D44 并发簇点击 .flash 短闪吸引注意 (agent-flash 动画)")
    check(".agent-row.sel" in css, "D44 .agent-row.sel 限时锁定样式 (区别 hover .hl)")
    check(bool(re.search(r'setTimeout\(\(\) =>.*?classList\.remove\("sel"\),\s*2600\)', appjs)),
          "D44 并发簇 .sel 限时淡出 (~2.6s 自动移除; 防一直高亮)")
    check(bool(re.search(r'function drillSpawn.*?querySelectorAll\("\.agent-row\.sel"\).*?remove\("sel"\)', appjs, re.S)),
          "D44 drillSpawn 进 spawn 详情 即清 .sel (点进去再返回不残留)")
    check(bool(re.search(r'querySelector\("\.gantt"\).*?classList\.contains\("multi"\).*?drillSpawn', appjs, re.S)),
          "D44 单段/单竖线 (非 multi) 点击仍 drillSpawn → spawn 详情 (并发簇与单点分流)")

    # agents 面板外 spawn 名统一带 #i (用户: "面板之外的地方名字都该带序号后缀, 同名太多"):
    # sync 段 tooltip / cache 书挡 / 拓扑 / 详情页标题 都带 #i 对回 agents 面板行; async tooltip 已有 (idxStr)
    check('_sessionCtx.idxByAgent' in appjs, "D44 idxByAgent 挂进 _sessionCtx (showSpawn 顶层取 #i)")
    check(bool(re.search(r'idxByAgent = \{\}; segs\.forEach.*?if \(bySkill\.length\)', appjs, re.S)),
          "D44 idxByAgent 无条件建 (在 bySkill 块外; 无 skill session 拓扑也能用, 防 ReferenceError)")
    check(bool(re.search(r'gantt-seg.{0,400}?segs\.indexOf\(s\)', appjs, re.S)),
          "D44 时间轴 sync 段 tooltip 带 #i (segs.indexOf(s))")
    check('class="row-idx"' in appjs, "D44 cache 书挡/拓扑 spawn 名后 #i (.row-idx; 对回 agents 行)")
    check('idxByAgent[aid]' in appjs, "D44 拓扑节点按 agentId 取时序 #i (idxByAgent[aid])")
    check('class="spawn-idx"' in appjs and '_sessionCtx.idxByAgent[agentId]' in appjs,
          "D44 详情页标题 spawn #i 主标识 (spawn-idx; showSpawn 从 ctx 取 idx)")
    check('唯一稳定锚点' in appjs, "D44 详情页 sid 降级保留 (title 注明唯一稳定锚点用途)")

    # 4) skill 切面 (SkillCall 事件维度): bySkill / skill-row / callerTypes×次数
    check("bySkill" in appjs and 'class="skill-row"' in appjs, "D44 skill 切面 bySkill + .skill-row")
    check("callerTypes" in appjs, "D44 skill callerTypes (调用方×次数)")
    # D9/Q3: skill turn chip = callerTurn 锚点 (root 调 → 紫 "root·tN"; subagent 调 → 蓝 "type#i·tN");
    # chip 带 data-agentid + data-turn, 点 root → drillRoot(turn) 进 spawn 详情; subagent → drillSpawn(agentId,turn) 进 spawn 详情 (定位 callerTurn; 与 root 对称, 非旧 drillTurn 直进 turn 原文 — showTurn 不隐藏 session 视图 致点不动).
    # 标题改 skills; grid 删 sess 废列 (恒=1) 成 4 列; #i 与 turn 合并进同一 chip 更紧凑.
    check("<h2>skills</h2>" in appjs, "D9 skill 面板标题 skills (原 skill 切面)")
    check("grid-template-columns:1.4fr 50px 56px 1fr" in css,
          "D9 skill grid 4 列 (删 sess 废列; 无第 5 列)")
    check('class="skill-turn' in appjs and 'data-turn="${t.turn}"' in appjs and 'data-agentid=' in appjs,
          "D9 skill turn chip (.skill-turn + data-agentid + data-turn; callerTurn 锚点)")
    check('drillRoot(turn.dataset.turn)' in appjs and 'drillSpawn(turn.dataset.agentid, turn.dataset.turn)' in appjs,
          "Q3 skill turn chip 点击: root → drillRoot 进 spawn 详情; subagent → drillSpawn(agentId,turn) 进 spawn 详情 (非旧 drillTurn 直进 turn 原文)")
    check(bool(re.search(r'skillListEl.*?addEventListener.*?\.skill-turn.*?drillSpawn\(turn\.dataset\.agentid', appjs, re.S)),
          "Q3 skillListEl 委托 .skill-turn subagent 分支 → drillSpawn → spawn 详情 (root 分支 drillRoot)")
    check(".skill-turn" in css and ".skill-turn.root" in css,
          "D9 .skill-turn / .skill-turn.root chip 样式 (subagent 蓝 / root 紫)")
    check("typeByAgent" in appjs, "D8 typeByAgent 映射 (skill turn chip #i 带 agent 类型名, 非裸序号)")

    # ⑤ 调用拓扑缩进树: byParent/callerAgentId 建树, branch 递归, 环保护防死循环, depth-2 诚实标
    check("byParent" in appjs and "callerAgentId" in appjs, "D44 拓扑 byParent + callerAgentId 建树")
    check("branch(" in appjs, "D44 branch() 递归遍历")
    check("seen.has(aid)" in appjs, "D44 环保护 (seen.has, observe don't crash)")
    check("depth-3+" in appjs and "须 live hook" in appjs,
          "D44 depth-2 诚实标 (agent→agent 嵌套须 live hook, §9.3#1)")
    check(bool(re.search(r'topoTreeEl.*?addEventListener.*?drillSpawn', appjs, re.S)),
          "D44 拓扑节点点击委托 drillSpawn → spawn 详情")
    # 拓扑长树部分折叠 (镜像 renderTurnList): 默认显前 TOPO_SHOW 个 spawn, 余下节点带 .topo-folded 隐, 插 .topo-fold 折叠条 (data-topofold) 点展开
    check('const TOPO_SHOW = 8' in appjs
          and 'class="topo-fold" data-topofold' in appjs
          and 'nodeHtml(n, "topo-folded")' in appjs,
          "T-折叠 前 TOPO_SHOW=8 个显, 余下 nodeHtml extra=topo-folded + 插 .topo-fold 折叠条")
    check('topoTreeEl.classList.toggle("topo-expanded")' in appjs
          and "data-rest=" in appjs,
          "T-折叠 点 .topo-fold → toggle .topo-expanded (展开/收起两态) + data-rest 存余数供文案重拼")
    check("▴ 收起" in appjs and "点展开全显" in appjs
          and ".topo-tree:not(.topo-expanded) .topo-node.topo-folded" in css,
          "T-折叠 折叠条常驻 toggle 文案 (⋯ 还有 N · 点展开全显 ↔ ▴ 收起) + CSS 未展开隐 folded")
    # 拓扑 spawn 锚点 ↗tN (caller→spawn 详情): 每节点后挂锚点, 点 → 调用方详情 定位 (depth-2=root / depth-3=父 spawn, 处理嵌套)
    check('class="topo-anchor' in appjs and 'data-caller=' in appjs and 'data-turn=' in appjs,
          "T-锚点 拓扑 spawn 锚点 .topo-anchor + data-caller + data-turn")
    check('↗t${nd.ct}' in appjs, "T-锚点 锚点显 ↗t{callerTurn} (nodeHtml nd.ct)")
    check('(nd.ct != null && nd.ct !== "")' in appjs,
          "T-锚点 callerTurn!=null 才显锚点 (反查失败/live 缺省 → 不留死链)")
    check('isRootCaller = !nd.callerAgentId' in appjs,
          "T-锚点 caller 由 callerAgentId 定 (None=root 主线; 否则父 spawn, 处理嵌套 depth-2/depth-3)")
    check('if (anchor.dataset.caller === "root") drillRoot(anchor.dataset.turn)' in appjs,
          "T-锚点 点锚点 caller=root → drillRoot(callerTurn) 进 root 详情")
    check('drillSpawn(anchor.dataset.caller, anchor.dataset.turn)' in appjs,
          "T-锚点 点锚点 caller=父 spawn → drillSpawn(父 spawn, callerTurn) 进父 spawn 详情 (嵌套)")
    check(bool(re.search(r'closest\("\.topo-anchor"\).*?return.*?closest\("\.topo-node', appjs, re.S)),
          "T-锚点 锚点先于节点判 + return (点锚点不触发点行, 点行 → 本 spawn 详情 不变)")
    check(".topo-anchor" in css and ".topo-anchor.root" in css,
          "T-锚点 CSS .topo-anchor / .topo-anchor.root (蓝=父 spawn 调 / 紫=root 主线调)")

    # ⑥ 渲染顺序: 时间轴 → twin(cache 书挡 + context 曲线) → agents → skill → 拓扑 → outlier
    i_ag = appjs.find("${agentsHtml}")
    i_sk = appjs.find("${skillHtml}")
    i_tp = appjs.find("${topoHtml}")
    check(0 <= i_ag < i_sk < i_tp, "D44 渲染顺序 agents → skill → 拓扑 (agents 不压顶遮 skill/拓扑)")

    # ⑦ 旧异步独列 (asyncListEl) 已删 —— async 折进 agents 面板消重
    check("asyncListEl" not in appjs, "D44 删旧异步独列 asyncListEl handler (折进 agents 面板)")

    # ⑧ CSS 配套
    for sel in ("#agents-panel", ".agent-row", ".skill-row", ".topo-node", ".gantt-async", ".agent-tag"):
        check(sel in css, f"D44 style.css 含 {sel}")
    check("auto auto 1fr auto auto" in css, "D44 agent-row 5 列网格 (tag/#i/type/meta/model; 加 #i 列)")


def test_terminal_stats_core():
    """D39: terminal_stats 单一计费口径核 —— 终态块去重四桶求和; 占位块(stop_reason=None, 携全量 input)排除防虚胖;
    无文件/无终态块→(None,None); model 取首条 assistant. offline+live 共此核 → live==离线口径."""
    from terminal_stats import terminal_stats
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, "agent-x.jsonl")
    # 同 message.id "m1" 拆 2 块: 中间块(stop=None, 携"全量 input" 9999 诱惑虚胖) + 终态块(end_turn 真计费);
    # 另一条 distinct 终态 message "m2". 期望: m1 占位块排除, m1终态 + m2 求和.
    _write_agent_transcript(p, [
        ("m1", None, {"input_tokens": 9999, "cache_read_input_tokens": 0,   # 占位: 全量 input, stop=None → 排除
                      "cache_creation_input_tokens": 0, "output_tokens": 0}),
        ("m1", "end_turn", {"input_tokens": 1000, "cache_read_input_tokens": 3000,  # 同 id 终态块 (prio 高 → 胜出)
                             "cache_creation_input_tokens": 0, "output_tokens": 50}),
        ("m2", "tool_use", {"input_tokens": 2000, "cache_read_input_tokens": 4000,
                             "cache_creation_input_tokens": 100, "output_tokens": 60}),
    ])
    model, u = terminal_stats(p)
    check(u is not None, "D39 有终态块 → usage 非 None")
    check(u["input"] == 3000, f"D39 终态块求和 input=1000+2000=3000 (占位 9999 排除), got {u['input']}")
    check(u["cacheRead"] == 7000, f"D39 cacheRead=3000+4000=7000, got {u['cacheRead']}")
    check(u["cacheCreation"] == 100, f"D39 cacheCreation=0+100=100, got {u['cacheCreation']}")
    check(u["output"] == 110, f"D39 output=50+60=110, got {u['output']}")
    check(model == "glm-5.1", f"D39 model 取首条 assistant, got {model}")
    # 无文件 → (None, None)
    m2, u2 = terminal_stats(os.path.join(tmp, "nope.jsonl"))
    check((m2, u2) == (None, None), "D39 无文件 → (None, None)")
    # 仅占位块 (无终态块) → usage=None
    p2 = os.path.join(tmp, "agent-none.jsonl")
    _write_agent_transcript(p2, [("m3", None, {"input_tokens": 5, "cache_read_input_tokens": 0,
                                                "cache_creation_input_tokens": 0, "output_tokens": 0})])
    _, u3 = terminal_stats(p2)
    check(u3 is None, "D39 仅占位块无终态块 → usage=None")


def test_reconcile_live_records():
    """D40: 读端补全 (live 专用) —— async launch 占位 + 历史无 tokenSource 记录 → agent 文件终态覆盖 (agentFile/complete);
    已 agentFile 跳过省读盘; 无 agent 文件占位保留; 非 SubagentCall 不动. 每 agentId 一条记录 → 覆盖到位不需去重."""
    from analyze import _reconcile_live_records
    tmp = tempfile.mkdtemp()
    sid = "ffff1111-2222-3333-4444-555566667777"
    proj = "fakeproj"
    sub = os.path.join(tmp, proj, sid, "subagents")
    os.makedirs(sub)
    # async1 (agent 文件存在 → 应补全): 真值 cacheRead 8000
    _write_agent_transcript(os.path.join(sub, "agent-async1.jsonl"),
        [("ma1", "end_turn", {"input_tokens": 1000, "cache_read_input_tokens": 8000,
                              "cache_creation_input_tokens": 0, "output_tokens": 80})])
    # hist2 (历史无 tokenSource, agent 文件存在 → 应补全): 真值 cacheRead 2000
    _write_agent_transcript(os.path.join(sub, "agent-hist2.jsonl"),
        [("mh2", "end_turn", {"input_tokens": 500, "cache_read_input_tokens": 2000,
                              "cache_creation_input_tokens": 0, "output_tokens": 40})])
    # nomatch3: 无 agent 文件 → 占位保留 (末轮值不动)
    recs = [
        {"recordType": "SubagentCall", "sessionId": sid, "spawned": {"agentId": "async1"},
         "tokenSource": "none", "capturePhase": "launch",
         "tokens": {"input": None, "output": None, "cacheCreation": None, "cacheRead": None, "total": None}},
        {"recordType": "SubagentCall", "sessionId": sid, "spawned": {"agentId": "hist2"},
         "tokenSource": None, "capturePhase": None,
         "tokens": {"input": 10, "output": 5, "cacheCreation": 0, "cacheRead": 100, "total": 115}},
        {"recordType": "SubagentCall", "sessionId": sid, "spawned": {"agentId": "nomatch3"},
         "tokenSource": "lastTurn", "capturePhase": "launch",
         "tokens": {"input": 7, "output": 3, "cacheCreation": 0, "cacheRead": 200, "total": 210}},
        {"recordType": "SkillCall"},   # 非 SubagentCall → 不动
    ]
    out = _reconcile_live_records([dict(r) for r in recs], projects_root=tmp)
    by_aid = {(r.get("spawned") or {}).get("agentId"): r
              for r in out if r.get("recordType") == "SubagentCall"}
    a1 = by_aid["async1"]
    check(a1["tokenSource"] == "agentFile", f"D40 async1 补全 → tokenSource=agentFile, got {a1.get('tokenSource')}")
    check(a1["capturePhase"] == "complete", f"D40 async1 → capturePhase=complete, got {a1.get('capturePhase')}")
    check(a1["tokens"]["cacheRead"] == 8000, f"D40 async1 cacheRead None→8000, got {a1['tokens']['cacheRead']}")
    check(a1["tokens"]["total"] == 9080, f"D40 async1 total=1000+80+0+8000=9080, got {a1['tokens']['total']}")
    h2 = by_aid["hist2"]
    check(h2["tokenSource"] == "agentFile", "D40 hist2(历史无 tokenSource 字段) 补全 → agentFile")
    check(h2["tokens"]["cacheRead"] == 2000, f"D40 hist2 cacheRead 100→2000, got {h2['tokens']['cacheRead']}")
    nm = by_aid["nomatch3"]
    check(nm["tokenSource"] == "lastTurn", "D40 nomatch3 无文件 → tokenSource 不动 (占位保留)")
    check(nm["tokens"]["cacheRead"] == 200, "D40 nomatch3 无文件 → token 不动 (末轮保留)")
    check([r for r in out if r.get("recordType") == "SkillCall"][0] == {"recordType": "SkillCall"},
          "D40 非 SubagentCall 记录不动")


def test_record_agent_live_fix():
    """D41: record.record_agent 同步 completed → agentFile 真值 (覆盖末轮 1.7x-17x 低估); async_launched → launch 占位.
    实证根因: tool_response.usage 只携末轮 API usage; agent 文件终态累计 (terminal_stats) 才真值."""
    import importlib.util
    hooks = os.path.join(HERE, "..", "hooks")
    spec = importlib.util.spec_from_file_location("ops_record_test", os.path.join(hooks, "record.py"))
    record = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(record)
    tmp = tempfile.mkdtemp()
    sid = "11111111-2222-3333-4444-555566667777"
    root_tp = os.path.join(tmp, sid + ".jsonl")   # 主线 transcript_path (dirname=tmp; _agent_terminal 派生用)
    sub = os.path.join(tmp, sid, "subagents")
    os.makedirs(sub)
    # 同步 agent 文件: 真值 cacheRead=162496 (末轮只 26880, 印证低估)
    _write_agent_transcript(os.path.join(sub, "agent-syncX.jsonl"),
        [("ms1", "tool_use", {"input_tokens": 20000, "cache_read_input_tokens": 120000,
                              "cache_creation_input_tokens": 0, "output_tokens": 1000}),
         ("ms2", "end_turn", {"input_tokens": 5000, "cache_read_input_tokens": 42496,
                              "cache_creation_input_tokens": 0, "output_tokens": 500})])
    # 同步完成 payload (末轮 usage cacheRead=26880 诱惑低估)
    payload_sync = {
        "tool_name": "Agent", "session_id": sid, "transcript_path": root_tp, "cwd": tmp,
        "tool_input": {"subagent_type": "Explore"},
        "tool_response": {"status": "completed", "agentId": "syncX", "agentType": "Explore",
                          "totalDurationMs": 42000, "totalTokens": 28662, "resolvedModel": "glm-5.1",
                          "usage": {"input_tokens": 1800, "cache_read_input_tokens": 26880,
                                    "cache_creation_input_tokens": 0, "output_tokens": 400}},
    }
    r = record.record_agent(payload_sync)
    check(r["tokenSource"] == "agentFile", f"D41 同步 completed → tokenSource=agentFile, got {r.get('tokenSource')}")
    check(r["capturePhase"] == "complete", f"D41 同步 → capturePhase=complete, got {r.get('capturePhase')}")
    check(r["tokens"]["cacheRead"] == 162496,
          f"D41 同步 cacheRead 覆盖为真值 162496 (末轮 26880), got {r['tokens']['cacheRead']}")
    check(r["tokens"]["input"] == 25000, f"D41 同步 input=20000+5000=25000, got {r['tokens']['input']}")
    check(r["success"] is True, "D41 同步 completed → success=True")
    # 异步 payload (usage 恒空 → 占位 none/launch)
    payload_async = {
        "tool_name": "Agent", "session_id": sid, "transcript_path": root_tp, "cwd": tmp,
        "tool_input": {"subagent_type": "Explore"},
        "tool_response": {"status": "async_launched", "agentId": "asyncY", "agentType": "Explore",
                          "totalDurationMs": None, "resolvedModel": "glm-5.1", "usage": {}},
    }
    r2 = record.record_agent(payload_async)
    check(r2["tokenSource"] == "none", f"D41 异步 → tokenSource=none, got {r2.get('tokenSource')}")
    check(r2["capturePhase"] == "launch", f"D41 异步 → capturePhase=launch, got {r2.get('capturePhase')}")
    check(r2["tokens"]["cacheRead"] is None, f"D41 异步 cacheRead=None (占位), got {r2['tokens']['cacheRead']}")
    check(r2["success"] is False, "D41 异步 → success=False")


def test_code_mtime_invalidation():
    """D42: tools/*.py mtime 变 → STATE 缓存失效重算. 修总览陈旧根因: /api/result 只盯源 .jsonl mtime,
    不盯计算核 .py → terminal_stats 重构 scan token 后总览落后 drill (137M vs 1.4M).
    测 _code_changed 逻辑 (隔离 tmp 文件, 不碰真实 tools/*.py mtime) + _code_watch_files 监听集 + handler 接线."""
    import importlib.util
    dash = os.path.join(HERE, "..", "dashboard")
    spec = importlib.util.spec_from_file_location("ops_server_d42", os.path.join(dash, "server.py"))
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    # D42a: _code_watch_files 监听真实计费核 tools/*.py (非空 + 含 analyze/terminal_stats + 全在 tools/ 下)
    cw = srv._code_watch_files()
    check(len(cw) >= 1, "D42a _code_watch_files 非空 (监听计费核 .py)")
    bases = {os.path.basename(f) for f in cw}
    check("analyze.py" in bases and "terminal_stats.py" in bases,
          f"D42a 监听集含 analyze.py + terminal_stats.py, got {bases}")
    check(all(f.endswith(os.sep + "tools" + os.sep + os.path.basename(f)) for f in cw),
          "D42a 监听集全在 tools/ 下")
    # D42b: _code_changed 逻辑 — 重定向监听到 tmp .py (隔离, 不 bump 真实 mtime)
    fd, tf = tempfile.mkstemp(suffix=".py")
    os.close(fd)
    orig = srv._code_watch_files
    try:
        srv._code_watch_files = lambda: [tf]
        srv._LAST_CODE_MTIME = srv._watch_max_mtime([tf])      # 设基线
        check(srv._code_changed() is False, "D42b 基线后 (文件未变) _code_changed=False")
        t = time.time() + 2
        os.utime(tf, (t, t))                                   # bump mtime 到未来 (跨 fs mtime 粒度)
        check(srv._code_changed() is True, "D42b touch 后 _code_changed=True (触发 STATE 重算)")
        srv._LAST_CODE_MTIME = srv._watch_max_mtime([tf])      # _refresh 重设基线
        check(srv._code_changed() is False, "D42b 重设基线后 _code_changed=False (收敛)")
    finally:
        srv._code_watch_files = orig
        os.path.isfile(tf) and os.remove(tf)
    # D42c: handler 接线 — /api/result 须在 _logdir_changed OR _code_changed 时 refresh
    src = open(os.path.join(dash, "server.py")).read()
    check("_code_changed()" in src, "D42c /api/result handler 含 _code_changed() (接 STATE 失效)")


def test_live_session_drill():
    """D43: live 源 drill-down 不再 400. 修前 _resolve_root_path live 无 scanDir → 400 'no scanDir'.
    修后 live 分支 glob AGENTINSIGHT_PROJECTS_ROOT/*/<sid>.jsonl (镜像 record.py/analyze reconcile).
    覆盖 session / spawn / turn 三级 drill 全绿 + 不存在 sid → 404 (非 400)."""
    import json as _json
    sid = "aaaabbbb-cccc-dddd-eeee-ffff00000043"
    agent_id = "agent-d43"
    # 1. live logdir (record.py JSONL): 一条 SubagentCall 记录引用 sid + spawnedAgentId
    logtmp = tempfile.mkdtemp()
    proj_live = os.path.join(logtmp, "demo-live")
    os.makedirs(proj_live, exist_ok=True)
    rec = _json.dumps({
        "schemaVersion": 1, "timestamp": "2026-06-19T10:00:00+08:00",
        "runId": sid, "projectName": "demo-live", "sessionId": sid, "toolUseId": "call-d43",
        "caller": {"agentId": None, "agentType": None, "isRoot": True},
        "recordType": "SubagentCall", "subagentType": "Explore",
        "spawned": {"agentId": agent_id, "agentType": "Explore"},
        "tokens": {"input": 100, "output": 50, "cacheCreation": 0, "cacheRead": 200, "total": 350},
        "durationMs": 5000, "resolvedModel": "glm-5.1", "success": True, "error": None,
    }, ensure_ascii=False)
    with open(os.path.join(proj_live, "2026-06-19.jsonl"), "w") as f:
        f.write(rec + "\n")
    # 2. projects root (AGENTINSIGHT_PROJECTS_ROOT 隔离): <proj>/<sid>.jsonl 主线 + subagents/agent-<id>.jsonl
    proot = tempfile.mkdtemp()
    proj_dir = os.path.join(proot, "-home-fakeproj-d43")
    os.makedirs(proj_dir, exist_ok=True)
    root_path = os.path.join(proj_dir, sid + ".jsonl")
    root_lines = [
        _json.dumps({"type": "assistant", "timestamp": "2026-06-19T10:00:00+08:00",
                     "message": {"role": "assistant",
                                 "usage": {"input_tokens": 3000, "cache_creation_input_tokens": 0,
                                           "cache_read_input_tokens": 6000, "output_tokens": 100}}}),
        _json.dumps({"timestamp": "2026-06-19T10:01:00+08:00", "sessionId": sid, "isSidechain": False,
                     "type": "assistant", "uuid": "u-d43", "message": {"role": "assistant"},
                     "toolUseResult": {"status": "completed", "agentId": agent_id, "agentType": "Explore",
                                       "totalDurationMs": 5000,
                                       "usage": {"input_tokens": 500, "output_tokens": 50,
                                                 "cache_creation_input_tokens": 0, "cache_read_input_tokens": 4500},
                                       "totalTokens": 5050}}),
    ]
    with open(root_path, "w") as f:
        f.write("\n".join(root_lines) + "\n")
    sub = os.path.join(proj_dir, sid, "subagents")
    os.makedirs(sub, exist_ok=True)
    _write_agent_transcript(os.path.join(sub, f"agent-{agent_id}.jsonl"),
        [("ms1", "end_turn", {"input_tokens": 500, "cache_read_input_tokens": 4500,
                              "cache_creation_input_tokens": 0, "output_tokens": 50})])
    # 3. 起 live server, 注入 AGENTINSIGHT_PROJECTS_ROOT = proot (隔离; 子进程继承)
    port = _free_port()
    old_env = os.environ.get("AGENTINSIGHT_PROJECTS_ROOT")
    os.environ["AGENTINSIGHT_PROJECTS_ROOT"] = proot
    try:
        proc = _start(port, f"live:{logtmp}")
        try:
            check(_wait_ready(port), "D43 server ready (live source)")
            # session drill: 修前 400 'no scanDir', 修后 200 + callChains
            s2, b2 = _get(port, f"/api/session/{sid}")
            check(s2 == 200, f"D43 session 视图 /api/session live → 200 (修前 400 'no scanDir'), got {s2}")
            check(len(_json.loads(b2).get("callChains", [])) >= 1, "D43 session 视图 callChains 非空 (≥1 spawn)")
            # spawn drill
            s3, _ = _get(port, f"/api/spawn/{sid}/{agent_id}")
            check(s3 == 200, f"D43 spawn 详情 /api/spawn live → 200, got {s3}")
            # turn drill
            s4, _ = _get(port, f"/api/turn/{sid}/{agent_id}/0")
            check(s4 == 200, f"D43 turn 原文 /api/turn live → 200, got {s4}")
            # 不存在 sid → 404 (非 400)
            s5, _ = _get(port, "/api/session/deadbeef-0000-0000-0000-000000000000")
            check(s5 == 404, f"D43 live 不存在 sid → 404 (非 400), got {s5}")
        finally:
            proc.terminate(); proc.wait()
    finally:
        if old_env is None:
            os.environ.pop("AGENTINSIGHT_PROJECTS_ROOT", None)
        else:
            os.environ["AGENTINSIGHT_PROJECTS_ROOT"] = old_env


def test_skill_caller_turn_binding():
    """D6 (A2/D4 语义): callerTurn 绑定到含该 tool_use 的 assistant **message 序号** (message.id dedup 空间),
    非时序上最近的 assistant 行, 亦非 Skill result 行. 每条 tool_use(Skill) 必落在某 message → callerTurn 恒可绑;
    无配对 tool_result 的 tool_use → EOF flush, success=None (诚实缺省, 守流式大文件契约)."""
    from transcript_adapter import parse_transcript_file
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, "root.jsonl")
    with open(p, "w") as f:
        # m0 (turn_idx=0): 含 tool_use tu-s1 (skillA 触发)
        f.write(json.dumps({"type": "assistant", "timestamp": "2026-06-18T10:00:00+08:00",
                "message": {"id": "m0", "role": "assistant", "stop_reason": "tool_use",
                            "content": [{"type": "text", "text": "go"},
                                        {"type": "tool_use", "id": "tu-s1", "name": "Skill",
                                         "input": {"skill": "skillA"}}]}}) + "\n")
        # m1 (turn_idx=1): 纯 text 无 tool_use — 比 skillA result 时序更近, 证明绑定非"最近 assistant 行"
        f.write(json.dumps({"type": "assistant", "timestamp": "2026-06-18T10:00:01+08:00",
                "message": {"id": "m1", "role": "assistant", "stop_reason": "end_turn",
                            "content": [{"type": "text", "text": "done"}]}}) + "\n")
        # skillA result: tool_use_id tu-s1 命中 m0 → 配对 emit, callerTurn=0, success=True
        f.write(json.dumps({"type": "user", "timestamp": "2026-06-18T10:00:02+08:00",
                "message": {"role": "user", "content": [{"type": "tool_result",
                            "tool_use_id": "tu-s1", "content": "ok"}]},
                "toolUseResult": {"success": True, "commandName": "skillA"}}) + "\n")
        # m2 (turn_idx=2): 含 tool_use tu-s2 (skillB), 但**无配对 result 行** → EOF flush, success=None
        f.write(json.dumps({"type": "assistant", "timestamp": "2026-06-18T10:00:03+08:00",
                "message": {"id": "m2", "role": "assistant", "stop_reason": "tool_use",
                            "content": [{"type": "text", "text": "go2"},
                                        {"type": "tool_use", "id": "tu-s2", "name": "Skill",
                                         "input": {"skill": "skillB"}}]}}) + "\n")
    recs, _ = parse_transcript_file(p, None, True, "sid6", "proj6", {})
    sk = {r["skillName"]: r for r in recs if r.get("recordType") == "SkillCall"}
    check(len(sk) == 2 and "skillA" in sk and "skillB" in sk,
          f"D6 解析出 2 条 SkillCall (skillA 配对 emit + skillB EOF flush), got {sorted(sk)}")
    check(sk["skillA"].get("callerTurn") == 0,
          f"D6 skillA callerTurn=0 (绑定含 tu-s1 的 m0, 非时序最近 m1), got {sk['skillA'].get('callerTurn')}")
    check(sk["skillA"].get("success") is True,
          f"D6 skillA success=True (配对 tool_result 命中), got {sk['skillA'].get('success')}")
    check(sk["skillB"].get("callerTurn") == 2,
          f"D6 skillB callerTurn=2 (绑定含 tu-s2 的 m2, message 序号空间), got {sk['skillB'].get('callerTurn')}")
    check(sk["skillB"].get("success") is None,
          f"D6 skillB success=None (无配对 result → EOF flush 诚实缺省), got {sk['skillB'].get('success')}")
    check(sk["skillA"]["caller"]["agentId"] is None and sk["skillA"]["caller"]["isRoot"] is True,
          "D6 root caller (agentId=None, isRoot=True)")


def test_caller_turn_helper():
    """D10: record.py _caller_turn — 反查含 tool_use_id 的 assistant 行序号 (live SkillCall callerTurn).
    口径同 D14 (每 type==assistant + message dict 行 +1, 无 usage 过滤/无去重); 缺参/无文件/未命中 → None 不抛
    (hook 红线: always-on Skill 轨道绝不阻塞)."""
    hooks_dir = os.path.join(HERE, "..", "hooks")
    if hooks_dir not in sys.path:
        sys.path.insert(0, hooks_dir)
    import record
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, "agent-live.jsonl")
    with open(p, "w") as f:
        # row0 (turn_idx=0): 含 tool_use tu-1
        f.write(json.dumps({"type": "assistant", "timestamp": "2026-06-18T10:00:00+08:00",
                "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "tu-1",
                            "name": "Skill", "input": {}}]}}) + "\n")
        # row1 (turn_idx=1): 无 tool_use (计数仍 +1, 证明不跳过)
        f.write(json.dumps({"type": "assistant", "timestamp": "2026-06-18T10:00:01+08:00",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "mid"}]}}) + "\n")
        # row2 (turn_idx=2): 含 tool_use tu-2
        f.write(json.dumps({"type": "assistant", "timestamp": "2026-06-18T10:00:02+08:00",
                "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "tu-2",
                            "name": "Skill", "input": {}}]}}) + "\n")
    check(record._caller_turn(p, "tu-1") == 0, "D10 tu-1 → turn 0 (含该 tool_use 的首 assistant 行)")
    check(record._caller_turn(p, "tu-2") == 2, "D10 tu-2 → turn 2 (计数含无 tool_use 的中间行; 非 1)")
    check(record._caller_turn(p, "tu-nope") is None, "D10 未命中 tool_use_id → None")
    check(record._caller_turn(p, None) is None, "D10 缺 tool_use_id → None")
    check(record._caller_turn(None, "tu-1") is None, "D10 缺 transcript_path → None")
    check(record._caller_turn("/no/such/file.jsonl", "tu-1") is None, "D10 无文件 → None 不抛")


def test_root_sample_turnindex_alignment():
    """D15 (A2/D2.3): turnIndex 进 message.id dedup 序号空间, 与 sample 去重位次 i **同空间相等**
    (turnIndex==i==by_msg 序号); agent_turn_raw(turnIndex).ts == sample.ts 同 message 对齐仍成立.
    合成 fixture: 同 message id 拆多行 (中间块 stop_reason=None 占位, 终态块带真 usage 是 by_msg 赢家),
    验证: m1 两行 dedup 成一条 sample, 赢家取终态块 row2 的 ts/usage; turnIndex=1 是 **message 序号**
    (非赢家行物理序号 2) — 证明 dedup 已吸收占位行, 序号空间归一."""
    from transcript_adapter import root_context_samples, agent_turn_raw
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, "root.jsonl")
    # row0: m0 终态 (msg 序号 0); row1: m1 占位块 (物理 row1, prio=0 非赢家); row2: m1 终态块 (物理 row2, prio=1 赢家)
    rows = [
        {"type": "assistant", "timestamp": "2026-06-18T10:00:00+08:00",
         "message": {"role": "assistant", "id": "m0", "stop_reason": "end_turn",
                     "usage": {"input_tokens": 100, "cache_read_input_tokens": 50,
                               "cache_creation_input_tokens": 0, "output_tokens": 5},
                     "content": [{"type": "text", "text": "t0"}]}},
        {"type": "assistant", "timestamp": "2026-06-18T10:00:01+08:00",
         "message": {"role": "assistant", "id": "m1", "stop_reason": None,
                     "usage": {"input_tokens": 100, "cache_read_input_tokens": 0,
                               "cache_creation_input_tokens": 0, "output_tokens": 2},
                     "content": [{"type": "text", "text": "mid"}]}},
        {"type": "assistant", "timestamp": "2026-06-18T10:00:02+08:00",
         "message": {"role": "assistant", "id": "m1", "stop_reason": "end_turn",
                     "usage": {"input_tokens": 120, "cache_read_input_tokens": 80,
                               "cache_creation_input_tokens": 0, "output_tokens": 5},
                     "content": [{"type": "text", "text": "win"}]}},
    ]
    with open(p, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    samples = root_context_samples(p)["samples"]
    check(len(samples) == 2, f"D15 去重后 2 个 unique message (m0/m1), got {len(samples)}")
    check(samples[0]["i"] == 0 and samples[0]["turnIndex"] == 0,
          "D15 sample0 i=0 turnIndex=0 (m0 即首条 message, 序号 0)")
    # 关键: m1 两行 dedup 成 1 条, 赢家取终态 row2 (ts=...02, usage 120/80); turnIndex=1 是 **message 序号**
    # (非赢家行物理序号 2) — dedup 已吸收占位行, turnIndex==i 同空间归一
    check(samples[1]["i"] == 1 and samples[1]["turnIndex"] == 1,
          f"D15 sample1 i=1 turnIndex=1 (dedup 成 1 msg, 序号空间归一; 非赢家行物理 row2), "
          f"got i={samples[1]['i']} turnIndex={samples[1]['turnIndex']}")
    check(samples[1]["i"] == samples[1]["turnIndex"],
          "D15 turnIndex==i (A2 后同 message 序号空间, 前端 i 与 turnIndex 等价)")
    check(samples[1]["ts"] == "2026-06-18T10:00:02+08:00",
          f"D15 m1 赢家取终态块 row2 的 ts (stop_reason 优先+行序最大), got {samples[1]['ts']}")
    # 对齐: agent_turn_raw(turnIndex) 取回的 ts == sample.ts (同 assistant 行, MATCH)
    for sm in samples:
        raw = agent_turn_raw(p, sm["turnIndex"])
        check(raw is not None and raw.get("ts") == sm["ts"],
              f"D15 agent_turn_raw(turnIndex={sm['turnIndex']}).ts == sample.ts (同行对齐), "
              f"got raw_ts={(raw or {}).get('ts')} vs {sm['ts']}")


def test_skill_callerturn_thruflow_by_skill():
    """D7: callerTurn 贯通 parse → to_event → by_skill (offline 数据层端到端).
    root 直调 skill (callerAgentId=None, turn=0) 与 subagent 内调 skill (callerAgentId=agent-X, turn=1)
    两条: to_event 暴露 callerTurn; by_skill 收 turns=[{sessionId,agentId,agentType,turn}] (None 不进, agentId 留 root/sub 区分).
    证明 drillTurn 锚点 (callerAgentId, callerTurn) 与 by_skill turns 对接正确."""
    from transcript_adapter import parse_transcript_file
    from analyze import to_event, by_skill
    tmp = tempfile.mkdtemp()

    def _bound_skill_transcript(path, tu_id):
        """一行 assistant 含 tool_use(tu_id) turn_idx=0 + 一行 Skill result 绑定该 tu_id → callerTurn=0."""
        with open(path, "w") as f:
            f.write(json.dumps({"type": "assistant", "timestamp": "2026-06-18T10:00:00+08:00",
                    "message": {"role": "assistant", "stop_reason": "tool_use",
                                "content": [{"type": "tool_use", "id": tu_id, "name": "Skill",
                                             "input": {"skill": "x"}}]}}) + "\n")
            f.write(json.dumps({"type": "user", "timestamp": "2026-06-18T10:00:01+08:00",
                    "message": {"role": "user", "content": [{"type": "tool_result",
                                "tool_use_id": tu_id, "content": "ok"}]},
                    "toolUseResult": {"success": True, "commandName": "skillX"}}) + "\n")

    # root 主线 transcript (caller=root): skillA callerTurn=0
    root_p = os.path.join(tmp, "root.jsonl")
    _bound_skill_transcript(root_p, "tu-root")
    recs_root, _ = parse_transcript_file(root_p, None, True, "sid7", "proj7", {})
    # subagent 自己的 transcript (caller=agent-X): skillA callerTurn=0 (该 agent 内首 turn)
    sub_p = os.path.join(tmp, "agent-x.jsonl")
    _bound_skill_transcript(sub_p, "tu-sub")
    recs_sub, _ = parse_transcript_file(sub_p, "agent-X", False, "sid7", "proj7", {})

    evs = [to_event(r) for r in (recs_root + recs_sub) if r.get("recordType") == "SkillCall"]
    check(len(evs) == 2, f"D7 2 条 SkillCall event (root + subagent 各一), got {len(evs)}")
    ev_root = [e for e in evs if e.get("callerAgentId") is None][0]
    ev_sub = [e for e in evs if e.get("callerAgentId") == "agent-X"][0]
    check(ev_root.get("callerTurn") == 0, "D7 to_event root skill 暴露 callerTurn=0")
    check(ev_sub.get("callerTurn") == 0, "D7 to_event subagent skill 暴露 callerTurn=0 (该 agent 内首 turn)")

    bs = by_skill(evs)
    check(len(bs) == 1 and bs[0]["calls"] == 2, "D7 by_skill 1 skill 行 calls=2 (root+subagent 同名)")
    turns = bs[0]["turns"]
    # turns 项 = {sessionId, agentId, agentType, turn} (Task1 扩展; 字段级匹配不绑死完整形状)
    check(any(t.get("agentId") is None and t.get("turn") == 0 for t in turns),
          f"D7 by_skill turns 含 root 锚 {{agentId:None, turn:0}}, got {turns}")
    check(any(t.get("agentId") == "agent-X" and t.get("turn") == 0 for t in turns),
          f"D7 by_skill turns 含 subagent 锚 {{agentId:agent-X, turn:0}}, got {turns}")
    check(len(turns) == 2, "D7 by_skill turns 收 2 项 (每次调用一锚; None turn 不进则少)")


def test_root_observability():
    """D1-D15 + DR1-DR8: root 主线 (orchestrator) 成为 session 详情页一等公民 (静态文本契约) ——
    时间轴 root lane (离散紫点, D1/D12) + 时间跨度并入 root ts (D2) + idle 重定义 (D3) +
    drillTurn(agentId, turnIndex) 新签名 root sentinel "root" (D4; turn 原文 back 统一 backToSpawn 回 spawn 详情) +
    sparkline 按 ts 画 x 且逐点可点 (D15). root 点/sparkline 点/skill chip 一律 drillRoot 进 root
    详情 (DR5, 非旧 session 视图→turn 原文 直跳 drillTurn). turn 序号一律用 s.turnIndex (非 i)."""
    appjs = open(os.path.join(HERE, "..", "dashboard", "static", "app.js")).read()
    css = open(os.path.join(HERE, "..", "dashboard", "static", "style.css")).read()

    # D4/D11: drillTurn 新签名 (显式 agentId, root sentinel "root"; 不读全局 _spawnAgentId)
    check(bool(re.search(r'function drillTurn\(agentId,\s*turnIndex\)', appjs)),
          "D4 drillTurn(agentId, turnIndex) 新签名 (显式 agentId; root sentinel)")
    check('_turnOrigin = (agentId === "root") ? "root" : "spawn"' in appjs,
          "D4/D11 drillTurn 记 _turnOrigin (root=主线 / spawn=subagent)")
    check('let _turnOrigin = null;' in appjs and 'function backToSession()' in appjs,
          "D11 _turnOrigin 模块变量 + backToSession (root turn 无 spawn, 返 session)")
    check('const isRoot = _turnOrigin === "root";' in appjs,
          "D11 showTurn back-btn 按 _turnOrigin 切文案/回调")

    # D2: root turn ts 并入时间跨度 (root 在 subagent 包络外的活动不再被裁)
    check("(d.rootContext || {}).samples" in appjs and "rootTs.forEach(t => tlPts.push(t))" in appjs,
          "D2 rootContext.samples 的 ts 并入 tmin/tmax (root∪subagent 并集跨度)")

    # D1/D12: root lane 离散紫点 (诚实: 只表达"此刻 root 执行 turn", 不虚构时长)
    check('class="root-dot' in appjs and 'data-agentid="root"' in appjs,
          "D1 root lane 离散紫点 (.root-dot; data-agentid=root)")
    check('const ti = (s.turnIndex != null) ? s.turnIndex' in appjs and 'data-turn="${ti}"' in appjs,
          "D15 root 点用 s.turnIndex (非去重位次 s.i) 传 drillTurn")
    check("isPeak ? ' peak'" in appjs, "D12 ctx peak turn 点加琥珀环 (.root-dot.peak)")
    for sel in (".root-dot", ".root-dot.peak", ".root-dot:hover"):
        check(sel in css, f"D12 style.css 含 {sel} (root 紫点 hover 光晕 / peak 琥珀环)")

    # D3: idle 重定义 — 间隙含 root turn = "含 N root turn" (root 在活动, 非 idle); 仅真空 = "空闲 Nm"
    check("rootTs.filter(t => t > prev.end && t < s.start)" in appjs, "D3 间隙内 root turn 计数 (rn)")
    check('`含 ${rn} root turn`' in appjs and '`空闲 ${gapMin}m`' in appjs,
          "D3 idle 重定义文案 (含 root turn / 空闲)")
    check(".gantt-gap.has-root" in css, "D3 .gantt-gap.has-root 样式 (含 root turn 紫区别灰 idle)")

    # D15: sparkline 按 ts 画 x (非 turn 序号 i/(n-1)) + 逐点可点 → drillTurn("root", turnIndex)
    check("(Date.parse(s.ts) - tmin) / span * W" in appjs,
          "D15 sparkline x = (s.ts - tmin)/span (按 ts, 非序号 i/(n-1))")
    check('class="spark-pt" data-turn="${s.turnIndex}"' in appjs,
          "D15 sparkline .spark-pt data-turn = s.turnIndex (非 i)")
    check(".spark-pt" in css and ".spark" in css, "D15 .spark / .spark-pt 样式 (可点光标)")
    # root lane 点 / sparkline 点 一致用 turnIndex 路由 drillRoot (DR5: 进 root 详情, 不再 session 视图→turn 原文 直跳 drillTurn)
    check(bool(re.search(r'closest\("\.root-dot"\).*?drillRoot\(', appjs, re.S)),
          "D1/DR5 root lane 点 → drillRoot(turnIndex) → root 详情 (定位 turn)")
    check(bool(re.search(r'closest\("\.spark-pt"\).*?drillRoot\(', appjs, re.S)),
          "D15/DR5 sparkline 点 → drillRoot(turnIndex) → root 详情 (定位 turn)")


def test_root_detail_route():
    """DR2: GET /api/root/<sid> → {agentId:root, head, traces, depth2Note}. root 主线 详情后端通道
    (镜像 _handle_spawn). head.turnCount==traces.n (逐 turn caliber D14); head.peak/sum 取自 root_context_samples
    (与时间轴紫点 / sparkline 同源, 非 traces 推导). fixture: root jsonl 含 2 个 assistant usage turn (ctx 9000+3000)."""
    tmp = tempfile.mkdtemp()
    proj = os.path.join(tmp, "-home-fakeproj")
    sid = "ffff0000-1111-2222-3333-444455556666"  # UUID 形 (discover_root_transcripts 过滤)
    os.makedirs(proj, exist_ok=True)
    root_path = os.path.join(proj, sid + ".jsonl")
    lines = [
        # turn 0: usage ctx = input 3000 + cacheRead 6000 = 9000 (将是 peak)
        json.dumps({"type": "assistant", "timestamp": "2026-06-18T10:00:00+08:00",
                    "message": {"role": "assistant", "id": "msg-root-0", "stop_reason": "tool_use",
                                "usage": {"input_tokens": 3000, "cache_creation_input_tokens": 0,
                                          "cache_read_input_tokens": 6000, "output_tokens": 100}}}),
        # turn 1: usage ctx = input 1000 + cacheRead 2000 = 3000
        json.dumps({"type": "assistant", "timestamp": "2026-06-18T10:00:05+08:00",
                    "message": {"role": "assistant", "id": "msg-root-1", "stop_reason": "end_turn",
                                "usage": {"input_tokens": 1000, "cache_creation_input_tokens": 0,
                                          "cache_read_input_tokens": 2000, "output_tokens": 50}}}),
    ]
    with open(root_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    port = _free_port()
    proc = _start(port, f"scan:{tmp}")
    try:
        check(_wait_ready(port), "DR2 server ready (scan source)")
        status, body = _get(port, f"/api/root/{sid}")
        check(status == 200, f"DR2 /api/root/<sid> 200, got {status}")
        got = json.loads(body)
        check(got.get("agentId") == "root", "DR2 返 agentId=root sentinel (root 主线)")
        tr = got.get("traces") or {}
        check(tr.get("n") == 2, f"DR2 traces.n == 2 (两个 assistant turn), got {tr.get('n')}")
        check(len(tr.get("turns", [])) == 2, "DR2 traces.turns 非空 (逐 turn 列表)")
        head = got.get("head") or {}
        check(head.get("agentType") == "root", "DR2 head.agentType == root")
        check(head.get("turnCount") == tr["n"], "DR2 head.turnCount == traces.n (D14 同口径)")
        check(head.get("peak") == 9000,
              f"DR2 head.peak == 9000 (root_context_samples, 非 traces 推导), got {head.get('peak')}")
        s = head.get("sum") or {}
        check(s.get("input") == 4000 and s.get("cacheRead") == 8000,
              f"DR2 head.sum 三桶累加 (input 4000 / cacheRead 8000), got {s}")
        check(bool(got.get("depth2Note")), "DR2 depth2Note 在 (root 详情说明)")
        # 不存在 sid → 404 (_resolve_root_path 未命中)
        s404, _ = _get(port, "/api/root/nope-nope-nope")
        check(s404 == 404, "DR2 不存在 sid → 404")
    finally:
        proc.terminate(); proc.wait()


def test_root_detail_frontend():
    """DR1-DR8: root 详情页前端契约 (镜像 spawn 详情; root 不再 session 视图→turn 原文 直跳).
    drillRoot/showRoot 存在; showRoot 用 renderTurnList(_,_,focusTurn) 3 参 + scrollIntoView + .flash;
    renderTurnList 第 3 参 focusTurn + keep 含 i===focusTurn; tooltip 含 #${ti} + slice(11,19) (非 esc(s.ts));
    三入口 (root-dot/spark-pt/skill chip root 分支) 调 drillRoot; showTurn back-btn 统一 backToSpawn; .turn-row.flash 在 css."""
    appjs = open(os.path.join(HERE, "..", "dashboard", "static", "app.js")).read()
    css = open(os.path.join(HERE, "..", "dashboard", "static", "style.css")).read()

    # DR3: drillRoot + showRoot 存在 (镜像 drillSpawn/showSpawn); fetch /api/root/<sid>
    check(bool(re.search(r'function drillRoot\(', appjs)), "DR3 drillRoot 定义 (fetch /api/root → showRoot)")
    check(bool(re.search(r'function showRoot\(d,\s*focusTurn\)', appjs)), "DR3 showRoot(d, focusTurn) 定义")
    check('fetch("/api/root/"' in appjs, "DR3 drillRoot fetch /api/root/<sid>")

    # DR4: renderTurnList 第 3 参 focusTurn + keep 含 i===ftNum (focusTurn 来自 dataset 是字符串,
    # 与 array index i(number) 须同型比较, 否则焦点行恒 false 被折叠 → querySelector 找不到 → 入场不定位)
    check(bool(re.search(r'function renderTurnList\(turns,\s*forceAll,\s*focusTurn\)', appjs)),
          "DR4 renderTurnList(turns, forceAll, focusTurn) 三参签名")
    check(bool(re.search(r'ftNum\s*=\s*.*Number\(focusTurn\)', appjs)) and 'i === ftNum' in appjs,
          "DR4 keep(i) 含 i===ftNum (Number(focusTurn) 同型比较; focus 行不被折叠)")
    # DR4: showRoot 用 renderTurnList 三参 + 查 focus 行 + scrollIntoView + .flash (入场定位)
    check('renderTurnList(turns, false, focusTurn)' in appjs,
          "DR4 showRoot renderTurnList(_, _, focusTurn) 三参 (focus 透传)")
    check('.turn-row[data-turn="${focusTurn}"]' in appjs, "DR4 showRoot 查 focus 行 .turn-row[data-turn]")
    check('classList.add("flash")' in appjs and 'scrollIntoView({block:"center"})' in appjs,
          "DR4 focus 行 .flash + scrollIntoView({block:center}) 入场定位")

    # DR5: 三入口 (root-dot / spark-pt / skill chip root 分支) 一律 drillRoot
    check('drillRoot(rd.dataset.turn)' in appjs, "DR5 gantt root-dot → drillRoot")
    check('drillRoot(pt.dataset.turn)' in appjs, "DR5 sparkline-pt → drillRoot")
    check('if (turn.dataset.agentid === "root") drillRoot(turn.dataset.turn)' in appjs,
          "DR5 skill chip root 分支 → drillRoot")
    # Q3: skill chip subagent 分支改 drillSpawn → spawn 详情 (定位 callerTurn; 非旧 drillTurn 直进 turn 原文 — showTurn 不隐藏 session 视图致点不动)
    check('drillSpawn(turn.dataset.agentid, turn.dataset.turn)' in appjs,
          "Q3 skill chip subagent 分支 → drillSpawn → spawn 详情 (与 root 对称)")

    # DR6: showTurn back-btn 统一 backToSpawn; label 按 isRoot (root=root主线 / spawn)
    check('v.querySelector(".back-btn").addEventListener("click", backToSpawn)' in appjs,
          "DR6 showTurn back-btn 统一 backToSpawn (root 现经 spawn 详情, 不再 backToSession)")
    check('"← 返回 root 主线"' in appjs and '"← 返回 spawn"' in appjs,
          "DR6 back-btn label 按 isRoot (root 主线 / spawn)")
    # backToSession 函数保留 (showSpawn/showRoot 的 back-btn 仍回 session 视图)
    check('function backToSession()' in appjs, "DR6 backToSession 函数保留 (spawn/root 详情 back 回 session 视图)")

    # DR7: tooltip 含 turn 序号 #${ti} + 时间 = 相对 session 起点偏移 fmtDurMs(t-tmin) (0-起点时刻, 与 gantt-axis 同源);
    # 不再 slice(11,19) 绝对时分秒 / 不再 esc(s.ts) 完整 ISO (旧 bug)
    check('root turn #${ti}' in appjs, "DR7 tooltip 含 #${ti} turn 序号 (用户要的)")
    check('fmtDurMs(t - tmin)' in appjs, "DR7 tooltip 时间 = fmtDurMs(t-tmin) 相对起点偏移 (0-起点时刻值)")
    check('gantt-axis' in appjs and 'fmtDurMs(span' in appjs,
          "DR7 tooltip 与 gantt-axis 同源 (axis 0/half/full 刻度也用 fmtDurMs; 口径一致)")
    check('点进 root 详情' in appjs, "DR7 tooltip 文案 '点进 root 详情' (进 spawn 详情)")
    check('esc(s.ts)' not in appjs, "DR7 tooltip 不再用 esc(s.ts) 直显完整 ISO (像当前时间的 bug)")
    check('(s.ts||"").slice(11,19)' not in appjs, "DR7 tooltip 不再 slice(11,19) 绝对时分秒 (改 0-起点相对偏移)")

    # DR8: .turn-row.flash 复用 @keyframes agent-flash
    check('.turn-row.flash' in css, "DR8 style.css 含 .turn-row.flash (focus 入场短闪)")
    check('@keyframes agent-flash' in css, "DR8 @keyframes agent-flash 已存 (复用, 非新 keyframes)")


def test_generation_tag_frontend():
    """Phase 3 跨 session 续接 (§10.1) dashboard 轻量: gen-tag + gen-group 分组的前端契约.
    镜像 D12/DR8 静态文件读法 — 读 index.html/app.js/style.css 源, 断言续接可见产物落地
    (generationId != sid → ⟿ 续接 tag; #gen-group 勾选 → multiSession 折进一组).
    前端无 headless browser → 只验源码契约 (逻辑在 fleetRow + gen-group 分派)."""
    html = open(os.path.join(HERE, "..", "dashboard", "static", "index.html")).read()
    appjs = open(os.path.join(HERE, "..", "dashboard", "static", "app.js")).read()
    css = open(os.path.join(HERE, "..", "dashboard", "static", "style.css")).read()
    # index.html: #gen-group checkbox 在 by-session sec-head (默认关 → 零视觉回归)
    check('id="gen-group"' in html, "DG1 index.html 含 #gen-group checkbox (按 generation 分组开关)")
    check("gen-group-toggle" in html, "DG1 index.html checkbox 带 .gen-group-toggle label")
    # app.js: gen-tag 仅在 generationId != sid (有 carrier 缝合) 时显; gen-group 分派消费 result.generations
    check("gen-tag" in appjs, "DG2 app.js 含 .gen-tag 发射 (跨 session 续接标签)")
    check("r.generationId !== r.sid" in appjs, "DG2 app.js gen-tag 仅 generationId != sid 时显 (无 carrier 不显)")
    check("result.generations" in appjs, "DG2 app.js 消费 result.generations (跨 session 卷起数组)")
    check('"gen-head"' in appjs, "DG2 app.js 含 gen-head 组头行 (multiSession 分组头)")
    # style.css: gen-tag / gen-group-toggle / gen-head 三规则
    check(".gen-tag" in css, "DG3 style.css 含 .gen-tag 规则")
    check(".gen-group-toggle" in css, "DG3 style.css 含 .gen-group-toggle 规则")
    check("tr.gen-head" in css, "DG3 style.css 含 tr.gen-head 规则 (分组头行)")


if __name__ == "__main__":
    test_api_result_file_source()
    test_static_routes_and_scaffolding()
    test_scan_source_and_refresh()
    test_live_source()
    test_live_tail_mtime_poll()
    test_live_tail_frontend_contract()
    test_appjs_id_consistency()
    test_session_drill()
    test_agent_turn_traces()
    test_agent_turn_traces_multi_tool()
    test_agent_spawn_head()
    test_agent_turn_raw()
    test_agent_turn_raw_separated()
    test_spawn_route()
    test_turn_route()
    test_turn_route_root()
    test_session_drill_transcript()
    test_source_switch()
    test_source_switch_invalid()
    test_source_switch_frontend_contract()
    test_browse_endpoint()
    test_browse_frontend_contract()
    test_infer_source()
    test_source_autoinfer_frontend()
    test_hero_panel_ux()
    test_session_ctx_peak_transcript()
    test_trust_single_session_fallback()
    test_source_is_live_and_inject()
    test_logdir_delete_and_data_age()
    test_live_source_watch_inject_stale()
    test_live_source_logdir_delete_blindspot()
    test_ctx_limit_errors_reader()
    test_ctx_limit_errors_e2e_and_frontend()
    test_fleet_sort_and_footer()
    test_cache_context_union_count()
    test_dist_row_name_hover_fullname()
    test_cache_hit_unified_billing_caliber()
    test_root_usage_transcript_sum()
    test_root_usage_dedup_multiline_message()
    test_fleet_table_merged_caliber()
    test_session_drill_merged_caliber()
    test_spawn_agentfile_token_override()
    test_async_spawn_status_and_tokens()
    test_status_propagates_callchains()
    test_async_ui_section()
    test_session_facets()
    test_terminal_stats_core()
    test_reconcile_live_records()
    test_record_agent_live_fix()
    test_code_mtime_invalidation()
    test_live_session_drill()
    test_skill_caller_turn_binding()
    test_caller_turn_helper()
    test_root_sample_turnindex_alignment()
    test_skill_callerturn_thruflow_by_skill()
    test_root_observability()
    test_root_detail_route()
    test_root_detail_frontend()
    test_generation_tag_frontend()
    print(f"\n{PASSED} PASS / 0 FAIL")
