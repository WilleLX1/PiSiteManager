# Patch: fix SSE newline handling so real newlines are rendered instead of the literal "\n".
# Change sse_tail batching to emit one "data: ..." line *per* log line, then a blank line.
# Re-write the full file to /mnt/data/manager.py for a clean drop-in.

from pathlib import Path
import os, json, asyncio, subprocess, signal
from datetime import datetime
from typing import Dict, Any, List, Optional, AsyncGenerator

from fastapi import FastAPI, Request, Depends, HTTPException, Form
from fastapi.responses import HTMLResponse, PlainTextResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.status import HTTP_401_UNAUTHORIZED
from jinja2 import Template

BASE_DIR = Path(os.environ.get("PSM_BASE_DIR", os.getcwd()))
CONFIG_PATH = BASE_DIR / "config.json"
PID_DIR = Path(os.environ.get("PSM_PID_DIR", "/tmp/pisite_pids"))
PID_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_CONFIG = {"sites": [], "auth": {"username": "admin", "password": "password"}}
CONFIG: Dict[str, Any] = {}

def _apply_env_overrides(cfg: Dict[str, Any]) -> Dict[str, Any]:
    env_user = os.environ.get("PSM_USERNAME")
    env_pass = os.environ.get("PSM_PASSWORD")
    env_token = os.environ.get("PSM_TOKEN")
    cfg.setdefault("auth", {})
    if env_user: cfg["auth"]["username"] = env_user
    if env_pass: cfg["auth"]["password"] = env_pass
    if env_token: cfg["auth"]["token"] = env_token
    return cfg

def load_config() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        cfg = json.load(f)
    return _apply_env_overrides(cfg)

def atomic_write_config(cfg: Dict[str, Any]):
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    data = dict(cfg)
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    if CONFIG_PATH.exists():
        CONFIG_PATH.replace(CONFIG_PATH.with_suffix(".json.bak"))
    tmp.replace(CONFIG_PATH)

def assign_config():
    global CONFIG
    CONFIG = load_config()

assign_config()

def get_site(name: str) -> Optional[Dict[str, Any]]:
    for s in CONFIG.get("sites", []):
        if s.get("name") == name:
            return s
    return None

def unauthorized(detail="Not authenticated"):
    headers = {"WWW-Authenticate": "Basic"}
    return HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail=detail, headers=headers)

def parse_basic_auth(header_val: str):
    import base64
    try:
        scheme, b64 = header_val.split(" ", 1)
        if scheme.lower() != "basic":
            return None
        raw = base64.b64decode(b64.strip()).decode("utf-8", "ignore")
        if ":" not in raw:
            return None
        user, pwd = raw.split(":", 1)
        return user, pwd
    except Exception:
        return None

async def check_auth(request: Request):
    auth = CONFIG.get("auth", {})
    authz = request.headers.get("Authorization", "")
    token = auth.get("token") or os.environ.get("PSM_TOKEN")
    if token and authz.startswith("Bearer "):
        if authz.replace("Bearer ", "", 1).strip() == token:
            return True
        raise unauthorized()
    basic = parse_basic_auth(authz) if authz else None
    if basic:
        u, p = basic
        if u == auth.get("username") and p == auth.get("password"):
            return True
        raise unauthorized()
    if not auth.get("username") and not auth.get("password") and not token:
        return True
    raise unauthorized()

def tmux_available() -> bool:
    try:
        subprocess.run(["tmux", "-V"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return True
    except Exception:
        return False

def tmux_has_session(name: str) -> bool:
    try:
        subprocess.run(["tmux", "has-session", "-t", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return True
    except subprocess.CalledProcessError:
        return False
    except FileNotFoundError:
        return False

def which(cmd: str) -> Optional[str]:
    try:
        out = subprocess.check_output(["bash", "-lc", f"command -v {cmd} || true"]).decode().strip()
        return out or None
    except Exception:
        return None

def wrap_unbuffered(cmd: str) -> str:
    stdbuf = which("stdbuf")
    prefix = "export PYTHONUNBUFFERED=1; "
    if stdbuf:
        return f"{prefix}{stdbuf} -oL -eL {cmd}"
    return f"{prefix}{cmd}"

def tmux_start(name: str, cwd: str, cmd: str, logfile: Path) -> str:
    cwd_path = Path(cwd)
    if not cwd_path.exists():
        raise HTTPException(400, f"CWD does not exist: {cwd}")
    full = f"bash -lc '{wrap_unbuffered(cmd)} 2>&1 | tee -a {str(logfile)}'"
    subprocess.run(["tmux", "new-session", "-d", "-s", name, "-c", str(cwd_path), full], check=True)
    return f"Started {name} in tmux"

def tmux_stop(name: str) -> str:
    if tmux_has_session(name):
        subprocess.run(["tmux", "kill-session", "-t", name], check=True)
        return f"Stopped {name}"
    return f"Session {name} not running"

def pid_file(name: str) -> Path:
    return PID_DIR / f"{name}.pid"

def background_running(name: str) -> bool:
    pf = pid_file(name)
    if not pf.exists():
        return False
    try:
        pid = int(pf.read_text().strip())
    except Exception:
        return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False

def background_start(name: str, cwd: str, cmd: str, logfile: Path) -> str:
    cwd_path = Path(cwd)
    cwd_path.mkdir(parents=True, exist_ok=True)
    pf = pid_file(name)
    if background_running(name):
        return f"{name} already running (pid {pf.read_text().strip()})"
    full = f"bash -lc '{wrap_unbuffered(cmd)} 2>&1 | tee -a {str(logfile)}'"
    proc = subprocess.Popen(full, cwd=str(cwd_path), shell=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            preexec_fn=os.setsid)
    pf.write_text(str(proc.pid), encoding="utf-8")
    return f"Started {name} (pid {proc.pid})"

def background_stop(name: str) -> str:
    pf = pid_file(name)
    if not pf.exists():
        return f"No pid for {name}"
    try:
        pid = int(pf.read_text().strip())
    except Exception:
        pf.unlink(missing_ok=True)
        return f"Invalid pid file removed for {name}"
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        pf.unlink(missing_ok=True)
        return f"Stopped {name} (pid {pid})"
    except ProcessLookupError:
        pf.unlink(missing_ok=True)
        return f"Process not found. Cleared pid for {name}"
    except Exception as e:
        return f"Failed to stop {name}: {e}"

def site_logfile(site: Dict[str, Any]) -> Path:
    log_path = site.get("log", "activity.log")
    return Path(site.get("cwd", ".")) / log_path

def tail_file(path: Path, n: int = 200) -> List[str]:
    if not path.exists():
        return []
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = 4 * 1024
            data = b""
            while size > 0 and data.count(b"\n") <= n + 1:
                step = min(block, size)
                size -= step
                f.seek(size)
                data = f.read(step) + data
            text = data.decode("utf-8", "ignore").splitlines()
            return text[-n:]
    except Exception:
        return []

def site_status(site: Dict[str, Any]) -> Dict[str, Any]:
    name = site["name"]
    running_tmux = tmux_available() and tmux_has_session(name)
    running_bg = background_running(name)
    status = "stopped"
    mode = None
    if running_tmux:
        status = "running"
        mode = "tmux"
    elif running_bg:
        status = "running"
        mode = "background"
    return {
        "name": name, "status": status, "mode": mode,
        "port": site.get("port"), "cwd": site.get("cwd"),
        "cmd": site.get("cmd"), "log": str(site_logfile(site)),
    }

app = FastAPI(title="PiSite Manager", version="2.3.1")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# (Templates identical to the throttled version; omitted here for brevity in commentary, but included in file creation above)
DASHBOARD_TMPL = Template("""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>PiSite Manager</title>
<style>body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:24px}h1{margin-bottom:8px}.meta{color:#666;margin-bottom:16px}
table{width:100%;border-collapse:collapse;margin-top:12px}th,td{border-bottom:1px solid #e5e5e5;padding:10px;text-align:left;vertical-align:top}
.badge{padding:4px 8px;border-radius:999px;font-size:12px}.ok{background:#e6ffed;color:#027a48}.stop{background:#ffeaea;color:#b42318}
.btn{padding:6px 10px;border:1px solid #ccc;background:#fafafa;border-radius:8px;cursor:pointer;margin-right:6px}.btn:hover{background:#f0f0f0}
.row-actions{white-space:nowrap}.footer{margin-top:24px;color:#888;font-size:12px}code{background:#f6f6f6;padding:2px 6px;border-radius:6px}
.card{border:1px solid #eee;border-radius:12px;padding:12px;margin-top:16px;background:#fcfcfc}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:8px}
input[type=text],input[type=number]{width:100%;padding:6px;border:1px solid #ccc;border-radius:8px}label{font-size:12px;color:#555}</style></head><body>
<h1>PiSite Manager</h1><div class="meta">{{ now }} • tmux: {{ "available" if tmux else "not available" }} <button class="btn" onclick="reloadCfg()">Reload config</button></div>
<div class="card"><h3>Add Site</h3><div class="grid">
<div><label>Name</label><input id="name" type="text" placeholder="Unique name"></div>
<div><label>Port</label><input id="port" type="number" placeholder="49152"></div>
<div><label>Log file</label><input id="log" type="text" placeholder="activity.log"></div>
<div style="grid-column:1/-1"><label>CWD</label><input id="cwd" type="text" placeholder="/path/to/app"></div>
<div style="grid-column:1/-1"><label>Command</label><input id="cmd" type="text" placeholder="/usr/bin/python3 app.py"></div>
</div><div style="margin-top:8px">
<label><input type="checkbox" id="autostart"> autostart</label>
<label style="margin-left:16px"><input type="checkbox" id="autorestart"> autorestart</label>
<label style="margin-left:16px"><input type="checkbox" id="start_after_add" checked> start after add</label>
</div><div style="margin-top:8px"><button class="btn" onclick="addSite()">Add</button></div></div>
<table><thead><tr><th>Site</th><th>Status</th><th>Mode</th><th>Port</th><th>Command</th><th>Actions</th></tr></thead>
<tbody id="rows">{% for s in sites %}<tr data-name="{{ s.name }}"><td><strong>{{ s.name }}</strong><br><small>{{ s.cwd }}</small><br><small>log: <code>{{ s.log }}</code></small></td>
<td>{% if s.status=="running" %}<span class="badge ok">running</span>{% else %}<span class="badge stop">stopped</span>{% endif %}</td>
<td>{{ s.mode or "-" }}</td><td>{{ s.port or "-" }}</td><td><code>{{ s.cmd }}</code></td>
<td class="row-actions"><button class="btn" onclick="postAction('{{ s.name }}','start')">Start</button><button class="btn" onclick="postAction('{{ s.name }}','stop')">Stop</button>
<button class="btn" onclick="postAction('{{ s.name }}','restart')">Restart</button><a class="btn" href="/logs/{{ s.name }}">Logs</a>
<button class="btn" onclick="deleteSite('{{ s.name }}')">Delete</button></td></tr>{% endfor %}</tbody></table>
<div class="footer">Config: <code>{{ config_path }}</code></div>
<script>
async function reloadCfg(){const r=await fetch('/api/reload',{method:'POST'});alert(await r.text());location.reload();}
async function postAction(name,op){const r=await fetch('/action',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({name,op})});alert(await r.text());refresh();}
async function refresh(){const r=await fetch('/api/status');const d=await r.json();const m={};for(const s of d)m[s.name]=s;for(const tr of document.querySelectorAll('tr[data-name]')){const n=tr.getAttribute('data-name');const s=m[n];if(!s)continue;tr.querySelectorAll('td')[1].innerHTML=s.status==='running'?'<span class="badge ok">running</span>':'<span class="badge stop">stopped</span>';tr.querySelectorAll('td')[2].textContent=s.mode||'-';}}
async function addSite(){const p=new URLSearchParams({name:document.getElementById('name').value.trim(),cwd:document.getElementById('cwd').value.trim(),cmd:document.getElementById('cmd').value.trim(),port:document.getElementById('port').value.trim(),log:document.getElementById('log').value.trim()||'activity.log',autostart:document.getElementById('autostart').checked?'true':'false',autorestart:document.getElementById('autorestart').checked?'true':'false',start_after_add:document.getElementById('start_after_add').checked?'true':'false'});const r=await fetch('/api/sites',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:p});alert(await r.text());await reloadCfg();}
async function deleteSite(name){if(!confirm('Delete site '+name+'?'))return;const r=await fetch('/api/sites/delete',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({name})});alert(await r.text());await reloadCfg();}
setInterval(refresh,3000);
</script></body></html>""")

LOGS_TMPL = Template("""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Logs · {{ name }}</title>
<style>body{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;margin:16px}
h2{margin:0 0 8px 0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.bar{display:flex;gap:8px;align-items:center;margin-bottom:8px;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.btn{padding:6px 10px;border:1px solid #ccc;background:#fafafa;border-radius:8px;cursor:pointer}.btn:hover{background:#f0f0f0}
pre{background:#0b1020;color:#c8e1ff;padding:12px;border-radius:8px;white-space:pre-wrap;max-height:75vh;overflow:auto}
.meta{color:#666;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
code{background:#f6f6f6;padding:2px 6px;border-radius:6px}.small{font-size:12px;color:#9aa4c0}</style></head><body>
<h2>Logs · {{ name }}</h2><div class="meta">cwd: <code>{{ cwd }}</code> · logfile: <code>{{ logfile }}</code></div>
<div class="bar"><button class="btn" onclick="postAction('{{ name }}','restart')">Restart</button>
<button class="btn" onclick="postAction('{{ name }}','stop')">Stop</button>
<button class="btn" onclick="postAction('{{ name }}','start')">Start</button>
<button class="btn" onclick="copy()">Copy</button>
<button class="btn" id="pauseBtn" onclick="togglePause()">Pause</button>
<a class="btn" href="/">Back</a> <span class="small">Live updates capped to last 2000 lines</span></div>
<pre id="log"></pre>
<script>
const MAX_LINES=2000; let paused=false,useSSE=false;
const el=document.getElementById('log');
function togglePause(){paused=!paused;document.getElementById('pauseBtn').textContent=paused?'Resume':'Pause';}
let buf=[],dirty=false,rafPending=false;
function scheduleRender(){if(rafPending)return;rafPending=true;requestAnimationFrame(()=>{rafPending=false;if(!dirty||paused)return;dirty=false;el.textContent=buf.join('\\n');el.scrollTop=el.scrollHeight;});}
function onLines(lines){if(paused||!lines||!lines.length)return;buf.push(...lines);if(buf.length>MAX_LINES){buf.splice(0,buf.length-MAX_LINES);}dirty=true;scheduleRender();}
function startSSE(){try{const es=new EventSource('/stream/{{ name }}');useSSE=true;let pending=[],flushTimer=null;const flush=()=>{onLines(pending);pending=[];flushTimer=null;};es.onmessage=(e)=>{if(e.data==='__CLEAR__'){buf=[];el.textContent='';return;}pending.push(e.data);if(!flushTimer){flushTimer=setTimeout(flush,200);} };es.onerror=()=>{es.close();useSSE=false;startPoll();};}catch{startPoll();}}
async function fetchLogs(){const r=await fetch('/api/logs/{{ name }}?lines='+MAX_LINES);const t=await r.text();buf=t.split('\\n');if(buf.length>MAX_LINES){buf=buf.slice(-MAX_LINES);}dirty=true;scheduleRender();}
function startPoll(){fetchLogs();setInterval(()=>{if(!useSSE&&!paused)fetchLogs();},1000);}
async function postAction(name,op){const r=await fetch('/action',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({name,op})});alert(await r.text());}
function copy(){navigator.clipboard.writeText(el.textContent);}
startSSE();
</script></body></html>""")

@app.get("/", response_class=HTMLResponse)
async def dashboard(_: bool = Depends(check_auth)):
    sites = [site_status(s) for s in CONFIG.get("sites", [])]
    html = DASHBOARD_TMPL.render(sites=sites, now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), tmux=tmux_available(), config_path=str(CONFIG_PATH))
    return HTMLResponse(html)

@app.get("/logs/{name}", response_class=HTMLResponse)
async def logs(name: str, _: bool = Depends(check_auth)):
    site = get_site(name)
    if not site:
        raise HTTPException(404, "Site not found")
    html = LOGS_TMPL.render(name=name, cwd=site.get("cwd"), logfile=str(site_logfile(site)))
    return HTMLResponse(html)

@app.get("/api/status", response_class=JSONResponse)
async def api_status(_: bool = Depends(check_auth)):
    return JSONResponse([site_status(s) for s in CONFIG.get("sites", [])])

@app.get("/api/logs/{name}", response_class=PlainTextResponse)
async def api_logs(name: str, lines: int = 200, _: bool = Depends(check_auth)):
    site = get_site(name)
    if not site:
        raise HTTPException(404, "Site not found")
    logs = tail_file(site_logfile(site), n=lines)
    return PlainTextResponse("\n".join(logs))

@app.post("/api/reload", response_class=PlainTextResponse)
async def api_reload(_: bool = Depends(check_auth)):
    assign_config()
    return PlainTextResponse("Config reloaded")

@app.post("/api/sites", response_class=PlainTextResponse)
async def add_site(name: str = Form(...), cwd: str = Form(...), cmd: str = Form(...), port: Optional[str] = Form(None), log: str = Form("activity.log"), autostart: str = Form("false"), autorestart: str = Form("false"), start_after_add: str = Form("false"), _: bool = Depends(check_auth)):
    if not name or any(ch in name for ch in "/\\ \t\n\r"):
        raise HTTPException(400, "Invalid name (no spaces or slashes).")
    if get_site(name):
        raise HTTPException(409, "A site with that name already exists.")
    if not cwd or not Path(cwd).exists():
        raise HTTPException(400, f"CWD does not exist: {cwd}")
    if not cmd:
        raise HTTPException(400, "Command cannot be empty.")
    p_int = int(port) if port else None
    cfg = load_config(); cfg.setdefault("sites", [])
    cfg["sites"].append({"name": name, "cwd": cwd, "cmd": cmd, "port": p_int, "log": log or "activity.log", "autostart": autostart.lower()=="true", "autorestart": autorestart.lower()=="true"})
    atomic_write_config(cfg); assign_config()
    if start_after_add.lower()=="true":
        site = get_site(name); logfile = site_logfile(site)
        if tmux_available():
            if not tmux_has_session(name):
                tmux_start(name, cwd, cmd, logfile)
        else:
            background_start(name, cwd, cmd, logfile)
    return PlainTextResponse(f"Added site {name}" + (" and started" if start_after_add.lower()=="true" else ""))

@app.post("/api/sites/delete", response_class=PlainTextResponse)
async def delete_site(name: str = Form(...), _: bool = Depends(check_auth)):
    site = get_site(name)
    if not site:
        raise HTTPException(404, "Site not found")
    if tmux_available() and tmux_has_session(name): tmux_stop(name)
    else: background_stop(name)
    cfg = load_config(); cfg["sites"] = [s for s in cfg.get("sites", []) if s.get("name") != name]
    atomic_write_config(cfg); assign_config()
    try: pid_file(name).unlink(missing_ok=True)
    except Exception: pass
    return PlainTextResponse(f"Deleted site {name}")

@app.post("/action", response_class=PlainTextResponse)
async def action(name: str = Form(...), op: str = Form(...), _: bool = Depends(check_auth)):
    site = get_site(name)
    if not site:
        raise HTTPException(404, "Site not found")
    logfile = site_logfile(site)
    if op == "start":
        if tmux_available():
            if tmux_has_session(name): return PlainTextResponse(f"{name} already running in tmux")
            return PlainTextResponse(tmux_start(name, site["cwd"], site["cmd"], logfile))
        else:
            return PlainTextResponse(background_start(name, site["cwd"], site["cmd"], logfile))
    elif op == "stop":
        if tmux_available() and tmux_has_session(name): return PlainTextResponse(tmux_stop(name))
        else: return PlainTextResponse(background_stop(name))
    elif op == "restart":
        if tmux_available() and tmux_has_session(name): tmux_stop(name)
        else: background_stop(name)
        await asyncio.sleep(0.3)
        if tmux_available(): return PlainTextResponse(tmux_start(name, site["cwd"], site["cmd"], logfile))
        else: return PlainTextResponse(background_start(name, site["cwd"], site["cmd"], logfile))
    else:
        raise HTTPException(400, "Unknown op")

# --- Fixed SSE: send one 'data:' line per log line, no backslash-escaped newlines ---
async def sse_tail(path: Path) -> AsyncGenerator[bytes, None]:
    yield b"data: __CLEAR__\n\n"
    last_size = 0
    pending: List[str] = []
    last_flush = asyncio.get_event_loop().time()
    KEEPALIVE_EVERY = 10.0
    last_keepalive = asyncio.get_event_loop().time()
    while True:
        try:
            now = asyncio.get_event_loop().time()
            if path.exists():
                size = path.stat().st_size
                if size < last_size:
                    last_size = 0
                if size > last_size:
                    with path.open("rb") as f:
                        f.seek(last_size)
                        chunk = f.read(size - last_size)
                        last_size = size
                        # Split into actual lines
                        pending.extend(chunk.decode("utf-8", "ignore").splitlines())
            # Flush at most 4x/sec as proper SSE frames (one data line per log line)
            if pending and (now - last_flush) >= 0.25:
                # Build a single bytes payload with multiple 'data:' lines then a blank line
                parts = []
                for line in pending:
                    parts.append(f"data: {line}\n")
                parts.append("\n")
                payload = "".join(parts).encode("utf-8")
                yield payload
                pending.clear()
                last_flush = now
            # keepalive comment
            if (now - last_keepalive) >= KEEPALIVE_EVERY:
                yield b": keep-alive\n\n"
                last_keepalive = now
            await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(0.25)

@app.get("/stream/{name}")
async def stream_logs(name: str, _: bool = Depends(check_auth)):
    site = get_site(name)
    if not site:
        raise HTTPException(404, "Site not found")
    path = site_logfile(site)
    return StreamingResponse(sse_tail(path), media_type="text/event-stream")

async def watchdog_loop():
    await asyncio.sleep(1.0)
    while True:
        try:
            for site in CONFIG.get("sites", []):
                name = site["name"]
                logfile = site_logfile(site)
                wants_autostart = bool(site.get("autostart"))
                wants_autorestart = bool(site.get("autorestart"))
                is_running = (tmux_available() and tmux_has_session(name)) or background_running(name)
                if (wants_autostart or wants_autorestart) and not is_running:
                    if tmux_available():
                        try: tmux_start(name, site["cwd"], site["cmd"], logfile)
                        except Exception: background_start(name, site["cwd"], site["cmd"], logfile)
                    else:
                        background_start(name, site["cwd"], site["cmd"], logfile)
        except Exception:
            pass
        await asyncio.sleep(3.0)

@app.on_event("startup")
async def on_start():
    asyncio.create_task(watchdog_loop())

print(f"[PiSite Manager] Loaded config from {CONFIG_PATH}")
print(f"[PiSite Manager] Sites: {[s['name'] for s in CONFIG.get('sites', [])]}")
print(f"[PiSite Manager] tmux available: {tmux_available()}")
print("Patched manager.py: SSE newline handling fixed (no literal \\n), with batching + keepalive.")
