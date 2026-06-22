#!/usr/bin/env python3
"""agent-insight A 形态 dashboard · 薄 HTTP server (§8).

stdlib http.server, 零外部依赖。喂 result JSON 给前端 (analyze.py --json 产物 / 直读 file)。
端口: AGENTINSIGHT_PORT (默认 8765)。

数据源 (--source, env AGENTINSIGHT_SOURCE):
  file:PATH        直读 result JSON 文件 (测试 / 固定快照)
  scan             analyze.py --scan-projects --json   (默认, 扫 ~/.claude/projects)
  scan:DIR         analyze.py --scan-projects DIR --json
  transcript:PATH  analyze.py --transcript PATH --json
  jsonl:PATH       analyze.py --jsonl PATH --json
  live             analyze.py --json   (Mode A, 扫 ~/.claude/agent-insight = record.py live 输出)
  live:DIR         analyze.py --logdir DIR --json   (指定 live logdir)

裸 path (无 prefix, 用户粘贴/浏览选定) → _infer_source 自动推断 (目录→scan, 在 live logdir 基下→live,
.jsonl 文件→transcript; 其他→拒). 高级用户仍可用上述 prefix 直接指定 (_KNOWN_PREF).
GET /api/presets → 常用 path 收藏 (scan + live + ~/.claude/projects 下各 <proj>), 前端下拉友好名.

红线: 只消费 analyze.py --json 产物 (已聚合, 零 prompt/content, F9 安全);
      源失败保留上一份缓存 + /api/result 返 503, 永不 crash。
"""
import glob
import json
import os
import subprocess
import sys
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")
ANALYZE = os.path.join(HERE, "..", "tools", "analyze.py")
sys.path.insert(0, os.path.join(HERE, "..", "tools"))   # import transcript_adapter readers: discover_root_transcripts (sid→path) + spawn head/traces/raw (spawn 详情/turn 原文)
try:
    from transcript_adapter import (discover_root_transcripts, agent_spawn_head,
                                    agent_turn_traces, agent_turn_raw, root_context_samples,
                                    count_tool_errors, count_ctx_limit_errors)
except ImportError:
    discover_root_transcripts = agent_spawn_head = agent_turn_traces = agent_turn_raw = root_context_samples = count_tool_errors = count_ctx_limit_errors = None
DEFAULT_PORT = 8765
BROWSE_ROOT = os.path.expanduser(os.environ.get("AGENTINSIGHT_BROWSE_ROOT", "~"))   # /api/browse 可信根 (默认 home)


def run_source(source):
    """按 source 规范产出 result JSON (dict). 源失败 → 抛异常 (上层记, 保留上一份缓存)."""
    if source.startswith("file:"):
        with open(source[len("file:"):], encoding="utf-8") as f:
            return json.load(f)
    cmd = [sys.executable, ANALYZE]
    if source == "scan":
        cmd += ["--scan-projects", "--json"]
    elif source.startswith("scan:"):
        cmd += ["--scan-projects", source[len("scan:"):], "--json"]
    elif source.startswith("transcript:"):
        cmd += ["--transcript", source[len("transcript:"):], "--json"]
    elif source.startswith("jsonl:"):
        cmd += ["--jsonl", source[len("jsonl:"):], "--json"]
    elif source == "live":
        cmd += ["--json"]                       # Mode A 默认 logdir (~/.claude/agent-insight, record.py live 输出; E1 后吐 perSession)
    elif source.startswith("live:"):
        cmd += ["--logdir", source[len("live:"):], "--json"]
    else:
        raise ValueError(f"unknown source: {source!r}")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"analyze.py exit {proc.returncode}: {proc.stderr.strip()[:500]}")
    return json.loads(proc.stdout)


def _source_is_live(source):
    """source 是否实时 live 源 (server mtime poll 刷新 → 数据随跑随变).
    live/live: → True; scan/transcript/jsonl/file → False. own-JSONL 静态文件不算实时 (虽 modeLabel 同为 'A · own-JSONL')."""
    return source == "live" or source.startswith("live:")


_KNOWN_PREF = ("file:", "jsonl:", "transcript:", "scan:", "live:")


def _resolve_live_logdir():
    """live logdir 基址 (call-time 读 env, 便于测试覆盖). 镜像 record.py:76-81 优先级:
    AGENTINSIGHT_LOG_DIR > CLAUDE_PLUGIN_DATA > ~/.claude/agent-insight."""
    base = (os.environ.get("AGENTINSIGHT_LOG_DIR", "").strip()
            or os.environ.get("CLAUDE_PLUGIN_DATA", "").strip()
            or os.path.expanduser("~/.claude/agent-insight"))
    return os.path.realpath(base)


def _infer_source(raw):
    """裸 path → (prefixed_source | None, err). 带 prefix / 裸 scan|live → 原样 (向后兼容, 高级用户/预置项).
    裸目录: 在 live logdir 基下 → live:DIR; 否则 scan:DIR (discover_root_transcripts 格式自适应, projects 根/单 proj/任意含 session 目录皆可).
    裸 .jsonl 文件 → transcript:PATH; 其他文件 → 拒; 不存在 → 拒. 用 realpath (与 /api/browse 返回的已解析绝对路径一致)."""
    if not raw:
        return None, "missing/empty source"
    if raw.startswith(_KNOWN_PREF) or raw in ("scan", "live"):
        return raw, None
    p = os.path.expanduser(raw)
    if not os.path.exists(p):
        return None, f"path not found: {raw}"
    rp = os.path.realpath(p)
    if os.path.isdir(rp):
        live_root = _resolve_live_logdir()
        return ("live:" + rp, None) if (rp == live_root or rp.startswith(live_root + os.sep)) \
               else ("scan:" + rp, None)
    if os.path.isfile(rp):
        return ("transcript:" + rp, None) if rp.endswith(".jsonl") \
               else (None, f"unsupported file type (选目录或 .jsonl 文件): {raw}")
    return None, f"not a regular path: {raw}"


def _validate_source(source):
    """校验 + 解析 (零副作用, 不碰 SOURCE/STATE). 返 (ok, err, resolved).
    带 prefix / 裸 scan|live → 原有校验, resolved=原值; 裸 path → _infer_source 推断, resolved=推断值 (推断内已校验存在性+类型)."""
    if not source or not isinstance(source, str):
        return False, "missing/empty source", None
    file_pref = ("file:", "jsonl:", "transcript:")
    dir_pref = ("scan:", "live:")
    if source.startswith(file_pref):
        p = source.split(":", 1)[1]
        return ((True, None, source) if p and os.path.isfile(p)
                else (False, f"file not found: {p}", None))
    if source.startswith(dir_pref):
        p = source.split(":", 1)[1]
        return ((True, None, source) if p and os.path.isdir(p)
                else (False, f"dir not found: {p}", None))
    if source in ("scan", "live"):
        return True, None, source
    inferred, err = _infer_source(source)
    return (True, None, inferred) if inferred else (False, err, None)


class _State:
    def __init__(self):
        self.lock = threading.Lock()
        self.result = None
        self.error = None

    def get(self):
        with self.lock:
            return self.result, self.error

    def set(self, result, error=None):
        with self.lock:
            if result is not None:
                self.result = result
            self.error = error


STATE = _State()
SOURCE = "scan"  # main() 覆盖
_LAST_REFRESH_MTIME = None  # 上次 refresh 时的 watch max mtime (live-tail 基线, §8.8)
_LAST_CODE_MTIME = None     # 上次 refresh 时 tools/*.py max mtime; 计算核变 → STATE 缓存失效重算
_LAST_WATCH_FILES = None    # 上次 refresh 时 watch 文件集 (sorted tuple); 增/删/改名 → 集合变 → refresh (单比 mtime 不跟删除, 2026-06-21 补)


def _watch_jsonl_under(base):
    """base 下 .jsonl watch 集 (一层 + 两层自适应): 单 project 目录的 root transcript 在一层
    (<proj>/<sid>.jsonl); projects 根在两层 (<root>/<proj>/<sid>.jsonl). reader (discover_root_transcripts)
    自适应找 transcript, watch 须同形 —— 旧版只 glob 两层, scan:<单proj> 源的一层 root transcript 命中 0 →
    server 永不 refresh (实证 2026-06-21: <proj>/*/*.jsonl=0 而 <proj>/*.jsonl=5 含活动 session)."""
    hits = glob.glob(os.path.join(base, "*.jsonl")) + glob.glob(os.path.join(base, "*", "*.jsonl"))
    return sorted(set(hits))


def _source_watch_files(source):
    """live-tail: 返回当前 source 读的文件列表 (只 stat mtime, 不读内容). mtime 变 → /api/result 自动 refresh.
    live/live: → <logdir> 下 .jsonl (record.py 按 date/projectName 滚动; 一层 + 两层皆覆盖).
    scan/scan: → <scan_dir> 下 .jsonl (顶层 root transcripts; 单 project 目录一层 / projects 根两层皆命中).
    jsonl:/transcript:/file: → 单文件.
    解析失败 / 无匹配 → [] (live-tail 静默降级到手刷 /api/refresh)."""
    if source.startswith("file:"):
        p = source[len("file:"):]
        return [p] if os.path.isfile(p) else []
    if source.startswith("jsonl:"):
        p = source[len("jsonl:"):]
        return [p] if os.path.isfile(p) else []
    if source.startswith("transcript:"):
        p = source[len("transcript:"):]
        return [p] if os.path.isfile(p) else []
    if source == "live":
        return _watch_jsonl_under(os.path.expanduser("~/.claude/agent-insight"))
    if source.startswith("live:"):
        return _watch_jsonl_under(source[len("live:"):])
    if source == "scan":
        return _watch_jsonl_under(os.path.expanduser("~/.claude/projects"))
    if source.startswith("scan:"):
        return _watch_jsonl_under(source[len("scan:"):])
    return []


def _watch_max_mtime(files):
    """files → 存在文件 max mtime (float); 空/全不存在 → None."""
    mt = None
    for f in files:
        try:
            m = os.path.getmtime(f)
        except OSError:
            continue
        if mt is None or m > mt:
            mt = m
    return mt


def _watch_data_age(source):
    """watch 文件最新 mtime 距今秒数 (数据活性, 前端 chip 据此判 实时/静止); 无文件 → None.
    实时算 (每次请求 now - mtime), 非 缓存态 —— 文件由静转动下次轮询即反映, 不卡死
    (区别于 _logdir_changed 的 mtime 基线比较, 那个跟删除有盲区)."""
    cur = _watch_max_mtime(_source_watch_files(source))
    return None if cur is None else (time.time() - cur)


def _code_watch_files():
    """聚合计算核 .py (tools/*.py) mtime 监听集. 这些文件变了 → 结果口径/逻辑变 → /api/result 须重算.
    防止 STATE 缓存 serving 旧口径而 drill (run_source 直跑, 永取当前码) 取新口径的不一致 —— 实证 2026-06-19:
    terminal_stats 重构 scan 每-session token 后, /api/result 总览 gt.cr=1.4M (陈旧缓存) vs /api/session drill 137M,
    因 watch 当时只盯源 .jsonl mtime 不盯 .py. user: 'session 页数据按新思路改了但总览还是旧的'."""
    return sorted(glob.glob(os.path.join(HERE, "..", "tools", "*.py")))


def _logdir_changed(source):
    """watch-files 是否变化 (纯 check; _refresh 负责更新基线).
    首次 (_LAST_REFRESH_MTIME is None) → False (启动 _refresh 尚未建立基线, 用启动快照).
    mtime 增长 OR 文件集变化 (增/删/改名) → True —— 单比 mtime 不跟删除 (删文件 mtime 不增 → 漏), 故并比集合."""
    if _LAST_REFRESH_MTIME is None:
        return False
    files = _source_watch_files(source)
    if _LAST_WATCH_FILES is not None and tuple(files) != _LAST_WATCH_FILES:
        return True
    cur = _watch_max_mtime(files)
    return cur is not None and cur > _LAST_REFRESH_MTIME


def _code_changed():
    """tools/*.py max mtime 是否新于上次 refresh (纯 check; _refresh 负责更新基线).
    首次 (基线 None) → False. 计算核 (analyze/terminal_stats/transcript_adapter) 变 → 结果口径变 →
    必须重算, 否则 /api/result (STATE 缓存) 落后于 /api/session|spawn|turn (run_source 直跑取当前码)."""
    if _LAST_CODE_MTIME is None:
        return False
    cur = _watch_max_mtime(_code_watch_files())
    return cur is not None and cur > _LAST_CODE_MTIME


def _refresh():
    global _LAST_REFRESH_MTIME, _LAST_CODE_MTIME, _LAST_WATCH_FILES
    try:
        STATE.set(run_source(SOURCE), None)
        files = _source_watch_files(SOURCE)
        _LAST_REFRESH_MTIME = _watch_max_mtime(files)                    # live-tail 基线
        _LAST_WATCH_FILES = tuple(files)                                 # 文件集基线 (删文件也触发下次 refresh)
        _LAST_CODE_MTIME = _watch_max_mtime(_code_watch_files())         # 计算核基线 (防口径漂移致 /api/result 陈旧)
    except Exception as e:
        STATE.set(None, f"{type(e).__name__}: {e}")


def _switch_source(new_source):
    """运行时切源 (POST /api/source). 原子: 校验已过 → 设 SOURCE → _refresh; refresh 失败回滚旧 SOURCE + 重刷.
    返 (ok, err). ok=True 时 SOURCE 已生效 + STATE 是新源; ok=False 时 SOURCE/STATE 回到旧源 (调用方报 503)."""
    global SOURCE
    old = SOURCE
    SOURCE = new_source
    _refresh()
    _, error = STATE.get()
    if error:  # 新源 refresh 失败 → 回滚旧源 + 重刷 (保旧缓存, 不留半截空 STATE)
        SOURCE = old
        _refresh()
        return False, error
    return True, None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静默 (测试不要 stderr 噪音)
        pass

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/result":
            if _logdir_changed(SOURCE) or _code_changed():  # 源数据变 OR 计算核 .py 变 → 重算 (后者防 analyze 重构后 STATE 陈旧, 总览落后 drill)
                _refresh()
            result, error = STATE.get()
            if result is None:
                self._send_json({"error": "not ready", "detail": error}, code=503)
            else:
                payload = dict(result)   # 浅 copy: 注入 isLive / dataAgeSeconds 不污染 STATE 缓存
                payload["isLive"] = _source_is_live(SOURCE)
                payload["dataAgeSeconds"] = _watch_data_age(SOURCE)  # 数据活性 (距最新更新秒数); 前端 chip 实时/静止据此, 每次 live-tail 重算
                self._send_json(payload)
        elif path == "/api/refresh":
            _refresh()
            result, error = STATE.get()
            self._send_json(result if result is not None else {"error": "detail", "detail": error},
                            code=200 if result is not None else 503)
        elif path == "/api/source":
            self._send_json({"current": SOURCE})
        elif path == "/api/presets":
            self._handle_presets()
        elif path == "/api/browse":
            q = parse_qs(urlparse(self.path).query)
            self._handle_browse(q.get("dir", [None])[0])
        elif path.startswith("/api/session/"):
            self._handle_session(path[len("/api/session/"):])
        elif path.startswith("/api/spawn/"):
            self._handle_spawn(path[len("/api/spawn/"):])
        elif path.startswith("/api/root/"):
            self._handle_root(path[len("/api/root/"):])
        elif path.startswith("/api/turn/"):
            self._handle_turn(path[len("/api/turn/"):])
        elif path == "/":
            self._serve_static("index.html", "text/html; charset=utf-8")
        elif path.startswith("/static/"):
            name = os.path.basename(path)
            ctype = {"app.js": "application/javascript",
                     "style.css": "text/css"}.get(name, "application/octet-stream")
            self._serve_static(name, ctype)
        else:
            self._send(404, b"not found")

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/api/source":
            self._send(404, b"not found")
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            self._send_json({"error": "invalid JSON body"}, code=400)
            return
        new_source = body.get("source")
        ok, err, resolved = _validate_source(new_source)   # 校验 + 解析 (零副作用): 非法 → 400, SOURCE 不动
        if not ok:
            self._send_json({"error": err}, code=400)
            return
        ok, err = _switch_source(resolved)                 # 原子切 + refresh (切推断后真实 source); 失败回滚旧 SOURCE/缓存
        if not ok:
            self._send_json({"error": f"refresh failed (rolled back): {err}"}, code=503)
            return
        self._send_json({"current": SOURCE})

    def _handle_browse(self, raw_dir):
        """/api/browse?dir=X → X 下目录 + .jsonl 文件列表 (前端弹层导航, §8 运行时切源).
        浏览器原生 file picker 拿不到真实路径 (fakepath 安全铁律) → 走 server 读 FS 正路.
        安全: BROWSE_ROOT 可信根 + realpath 穿越防护 (.. 逃逸 / 绝对外路径一律拒). raw_dir 缺省 → 根."""
        root = os.path.realpath(BROWSE_ROOT)
        target = os.path.realpath(raw_dir) if raw_dir else root
        if target != root and not target.startswith(root + os.sep):
            self._send_json({"error": f"dir out of browse root: {raw_dir}"}, code=400)
            return
        if not os.path.exists(target):
            self._send_json({"error": f"dir not found: {raw_dir}"}, code=400)
            return
        if not os.path.isdir(target):
            self._send_json({"error": f"not a directory: {raw_dir}"}, code=400)
            return
        parent = os.path.dirname(target) if target != root else None
        dirs, files = [], []
        try:
            for name in sorted(os.listdir(target)):
                full = os.path.join(target, name)
                if os.path.isdir(full):
                    dirs.append({"name": name, "isDir": True})
                elif name.endswith(".jsonl") and os.path.isfile(full):
                    files.append({"name": name, "isDir": False, "isJsonl": True,
                                  "size": os.path.getsize(full)})
        except OSError as e:
            self._send_json({"error": f"listdir failed: {e}"}, code=400)
            return
        self._send_json({"dir": target, "parent": parent, "entries": dirs + files})

    def _handle_presets(self):
        """GET /api/presets → 常用 path 收藏 (前端下拉, §8 友好化). scan + live + ~/.claude/projects 下每个 <proj>.
        友好名镜像 app.js projName: 去 '-home-' 前缀. project 列表每次现算 (~10 项, 无需缓存)."""
        import re
        presets = [{"label": "全部历史 session", "source": "scan"},
                   {"label": "实时编排", "source": "live"}]
        proj_root = os.path.expanduser("~/.claude/projects")
        if os.path.isdir(proj_root):
            for name in sorted(os.listdir(proj_root)):
                full = os.path.join(proj_root, name)
                if os.path.isdir(full):
                    label = re.sub(r"^-home-", "", name) or name
                    presets.append({"label": label, "source": "scan:" + full})
        self._send_json({"presets": presets, "current": SOURCE})

    def _resolve_root_path(self, sid):
        """sid → (root_path, error_msg, http_code).
        transcript 单文件源: root path = 当前 SOURCE 的 transcript 文件本身 (无需 scanDir 反查).
        live 源: 无 scanDir (record.py 记录跨 project 聚合), glob ~/.claude/projects/*/<sid>.jsonl.
        scan 源: 复用 discover_root_transcripts + STATE.scanDir 按 sid 反查.
        code: 200 ok / 404 not found / 400 no scanDir / 500 adapter 不可用."""
        if not discover_root_transcripts:
            return None, "transcript_adapter 不可用", 500
        # transcript 单文件源: drill session/spawn/turn 的 root 就是这个文件本身 (transcript 源无 scanDir, 无需反查)
        if SOURCE.startswith("transcript:"):
            root_path = SOURCE[len("transcript:"):]
            base = os.path.basename(root_path)
            name_sid = base[:-len(".jsonl")] if base.endswith(".jsonl") else base
            if name_sid == sid:
                return root_path, None, 200
            return None, f"sid not in current transcript: {sid}", 404
        # live 源: 无 scanDir (record.py 记录跨 project 聚合), 按 sid glob 主线 transcript.
        # 镜像 record.py:164 _agent_terminal glob 兜底 + analyze.py:154 reconcile 的 projects_root 优先级
        # (AGENTINSIGHT_PROJECTS_ROOT > ~/.claude/projects) —— 同一根保证 drill 取的 transcript 与 live 计费同源.
        # projectName (cwd basename) ≠ CC projects 目录名 (路径分隔符替换), 故不能拼、必须 glob.
        if _source_is_live(SOURCE):
            proot = (os.environ.get("AGENTINSIGHT_PROJECTS_ROOT", "").strip()
                     or os.path.expanduser("~/.claude/projects"))
            hits = sorted(glob.glob(os.path.join(proot, "*", sid + ".jsonl")))
            if not hits:
                return None, f"sid not found: {sid}", 404
            return hits[0], None, 200
        result, _ = STATE.get()
        scan_dir = (result or {}).get("scanDir")
        if not scan_dir:
            return None, "no scanDir (drill 需 scan 源)", 400
        match = [p for p in discover_root_transcripts(scan_dir)
                 if os.path.basename(p)[:-len(".jsonl")] == sid]
        if not match:
            return None, f"sid not found: {sid}", 404
        return match[0], None, 200

    def _handle_session(self, sid):
        """/api/session/<sid>: sid→root path → transcript: source (callChains + rootContext, Plan 3a)."""
        root_path, err, code = self._resolve_root_path(sid)
        if err:
            self._send_json({"error": err}, code=code)
            return
        try:
            data = run_source("transcript:" + root_path)
        except Exception as e:
            self._send_json({"error": f"{type(e).__name__}: {e}"}, code=503)
            return
        self._send_json(data)

    def _agent_path(self, root_path, sid, agent_id):
        """root path + sid + agentId → <proj>/<sid>/subagents/agent-<id>.jsonl (字符串派生, §8.6)."""
        return os.path.join(os.path.dirname(root_path), sid, "subagents", f"agent-{agent_id}.jsonl")

    def _handle_spawn(self, rest):
        """/api/spawn/<sid>/<agentId>: head (root toolUseResult) + traces (agent-*.jsonl). in-process 直读 (spawn 详情)."""
        parts = rest.split("/")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            self._send_json({"error": "expect /api/spawn/<sid>/<agentId>"}, code=404)
            return
        sid, agent_id = parts
        if not agent_spawn_head:
            self._send_json({"error": "transcript_adapter 不可用"}, code=500)
            return
        root_path, err, code = self._resolve_root_path(sid)
        if err:
            self._send_json({"error": err}, code=code)
            return
        agent_path = self._agent_path(root_path, sid, agent_id)
        if not os.path.isfile(agent_path):
            self._send_json({"error": f"agent transcript not found: {agent_id}"}, code=404)
            return
        try:
            head = agent_spawn_head(root_path, agent_id, agent_path=agent_path)
            traces = agent_turn_traces(agent_path)
            self._send_json({
                "sid": sid, "agentId": agent_id, "head": head, "traces": traces,
                "depth2Note": "agent 自己的工具调用离线可见; 孙 agent (depth-3 以上) 须 live hook",
            })
        except Exception as e:
            self._send_json({"error": f"{type(e).__name__}: {e}"}, code=503)

    def _handle_root(self, rest):
        """/api/root/<sid>: root 主线 traces (agent_turn_traces on root_path) + head. root 详情 (镜像 _handle_spawn).

        root = orchestrator (caller agentId 缺失); root_path = 主线 session transcript 本身 (无 subagents/agent-*.jsonl).
        traces 复用 agent_turn_traces (单 isfile 守卫, root transcript 同是 assistant/user 交替 → 直接可用).
        head.peak/sum 取自 root_context_samples (与时间轴紫点 / sparkline 同源同口径; traces 不按 message 去重,
        从 traces 推峰值会与 viz 不一致). turnCount = traces.n (turn=一条 assistant message, 与 spawn 详情 列表同 caliber)."""
        parts = rest.split("/")
        if len(parts) != 1 or not parts[0]:
            self._send_json({"error": "expect /api/root/<sid>"}, code=404)
            return
        sid = parts[0]
        if not agent_turn_traces:
            self._send_json({"error": "transcript_adapter 不可用"}, code=500)
            return
        root_path, err, code = self._resolve_root_path(sid)
        if err:
            self._send_json({"error": err}, code=code)
            return
        if not os.path.isfile(root_path):
            self._send_json({"error": f"root transcript not found: {sid}"}, code=404)
            return
        try:
            traces = agent_turn_traces(root_path)
            head = {"agentType": "root", "turnCount": traces["n"], "peak": None,
                    "sum": {"input": 0, "cacheCreation": 0, "cacheRead": 0}}
            if root_context_samples:   # 峰值/累计 ctx: 与时间轴紫点 + sparkline 同源同口径 (非 traces 推导)
                rc = root_context_samples(root_path)
                head["peak"] = rc.get("peak")
                head["sum"] = rc.get("sum", head["sum"])
            # §8.6 ✗ tool 失败 (root 主线 is_error 计数, 与 status 分轨) + §8.3 💥 ctx 爆掉 (顶层 API Error).
            # root 详情页 meta-chips 据此显 ✗N / 💥; 与 spawn 详情 head.toolErrorCount 对称 (spawn 经 agent_spawn_head 已带).
            head["toolErrorCount"] = count_tool_errors(root_path)["count"] if count_tool_errors else 0
            head["ctxLimitErrors"] = count_ctx_limit_errors(root_path) if count_ctx_limit_errors else {"count": 0, "sample": None}
            self._send_json({
                "sid": sid, "agentId": "root", "head": head, "traces": traces,
                "depth2Note": "root = 顶层 orchestrator (主线); 每个 turn = 一条助手消息. 点 turn 行→看原文.",
            })
        except Exception as e:
            self._send_json({"error": f"{type(e).__name__}: {e}"}, code=503)

    def _handle_turn(self, rest):
        """/api/turn/<sid>/<agentId>/<i>: 一个 assistant turn 原文 (logs, F9 on-demand, turn 原文)."""
        parts = rest.split("/")
        if len(parts) != 3 or not all(parts):
            self._send_json({"error": "expect /api/turn/<sid>/<agentId>/<i>"}, code=404)
            return
        sid, agent_id, idx_s = parts
        try:
            idx = int(idx_s)
        except ValueError:
            self._send_json({"error": f"turn index not int: {idx_s}"}, code=404)
            return
        if not agent_turn_raw:
            self._send_json({"error": "transcript_adapter 不可用"}, code=500)
            return
        root_path, err, code = self._resolve_root_path(sid)
        if err:
            self._send_json({"error": err}, code=code)
            return
        # D5: root sentinel → 直接用 root transcript (root 主线 turn 也可 钻取)
        if agent_id == "root":
            agent_path = root_path
        else:
            agent_path = self._agent_path(root_path, sid, agent_id)
        if not os.path.isfile(agent_path):
            self._send_json({"error": f"agent transcript not found: {agent_id}"}, code=404)
            return
        try:
            raw = agent_turn_raw(agent_path, idx)
            if raw is None:
                self._send_json({"error": f"turn {idx} not found"}, code=404)
                return
            self._send_json({"sid": sid, "agentId": agent_id, **raw})
        except Exception as e:
            self._send_json({"error": f"{type(e).__name__}: {e}"}, code=503)

    def _serve_static(self, name, ctype):
        full = os.path.join(STATIC_DIR, name)
        if not os.path.isfile(full):
            self._send(404, b"not found")
            return
        with open(full, "rb") as f:
            self._send(200, f.read(), ctype)


def main():
    global SOURCE
    import argparse
    ap = argparse.ArgumentParser(prog="server.py",
                                 description="agent-insight A 形态 dashboard server (§8).")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("AGENTINSIGHT_PORT", DEFAULT_PORT)))
    ap.add_argument("--source", default=os.environ.get("AGENTINSIGHT_SOURCE", "scan"))
    args = ap.parse_args()
    SOURCE = args.source
    _refresh()  # 启动即拉一次 (失败不退出, /api/result 返 503 + error)
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"agent-insight dashboard → http://127.0.0.1:{args.port}  (source={SOURCE})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()
