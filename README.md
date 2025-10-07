# PiSite Manager

A tiny FastAPI-based web UI to **start, stop, restart, and monitor multiple app/sites** on a Linux box (Raspberry Pi friendly).
It prefers **tmux** sessions for process management and falls back to a **background (PID) mode** when tmux isn't available.
Includes a live log viewer powered by **Server-Sent Events (SSE)** with a polling fallback.

---

## Highlights

* 🖥️ Web dashboard to add/remove sites and run common actions
* 🧰 Works with **tmux** or a portable **background** runner (PID file)
* 📜 Live logs: streams tail output via SSE, with copy/pause
* 🔁 Optional **autostart** and **autorestart** watchdog
* 🔐 Simple **Basic** or **Bearer** token auth (via config or env)
* ⚙️ Single JSON config (`config.json`) with hot reload

---

## Quick start

```bash
# 1) Clone and enter
git clone https://Github.com/WilleLX1/PiSiteManager.git
cd PiSiteManager

# 2) (Recommended) Python venv
python3 -m venv .venv
source .venv/bin/activate

# 3) Install deps
pip install -r requirements.txt

# 4) Optional: set auth via env file (see "Authentication" below)
#    Example: echo 'PSM_USERNAME=admin\nPSM_PASSWORD=changeme' > auth.env
export $(grep -v '^#' auth.env | xargs) || true

# 5) Run
uvicorn manager:app --host 0.0.0.0 --port 8088 --workers 1
```

Open: `http://<your-pi-ip>:8088/`

> Tip: If you already used it once, just run `run.sh`:
>
> ```bash
> sudo chmod +x run.sh
> ./run.sh
> ```

---

## Configuration

PiSite Manager stores configuration in `config.json` (created on first run).
Environment variables can override auth at runtime.

### `config.json` schema

```jsonc
{
  "sites": [
    {
      "name": "MySite",
      "cwd": "/home/pi/myapp",
      "cmd": "/usr/bin/python3 app.py",
      "port": 5000,                    // optional, for display only
      "log": "activity.log",           // relative to cwd
      "autostart": true,               // optional
      "autorestart": false             // optional
    }
  ],
  "auth": {
    "username": "admin",
    "password": "password",
    "token": null                      // optional Bearer token
  }
}
```

* `name` must be unique (no spaces or slashes).
* `cwd` must exist on disk.
* `cmd` is executed inside `cwd`.
* `log` is appended to by the process (we pipe stdout/stderr to it).
* `autostart`/`autorestart` are honored by an internal watchdog.

### Environment variables (override auth/config)

* `PSM_BASE_DIR` – where `config.json` is stored (default: current working dir)
* `PSM_PID_DIR` – PID files directory for background mode (default: `/tmp/pisite_pids`)
* `PSM_USERNAME` – overrides `auth.username`
* `PSM_PASSWORD` – overrides `auth.password`
* `PSM_TOKEN` – sets/overrides Bearer token

> Changes via env vars apply on next start or after **Reload config** (see API).

---

## Process model

### Preferred: tmux

If `tmux` is installed, each site runs in its own named session (`<site.name>`).
Start/stop/restart map directly to tmux session lifecycle.

### Fallback: background (PID)

If `tmux` is missing, a background process is launched with:

* unbuffered output (`stdbuf` if available, plus `PYTHONUNBUFFERED=1`)
* stdout/stderr **tee**'d into the site log
* PID tracked in `PSM_PID_DIR/<name>.pid`
* polite group termination on stop/restart

You can mix both: if tmux becomes available later, new starts will use tmux.

---

## Live logs

* Page: **Logs** → `/logs/<name>`
* Shows last lines and then **streams** updates via SSE.
* If SSE cannot connect, it falls back to short-interval **polling**.
* Buttons: **Copy**, **Pause/Resume**, **Start/Stop/Restart`.

> Note: Log file path is `cwd/log` and is created/appended automatically.

---

## Web UI

* Dashboard `/` lists all sites with status (running/stopped) and mode (tmux/background)
* Add Site form (name/cwd/cmd/port/log + autostart/autorestart)
* Actions per row: **Start**, **Stop**, **Restart**, **Logs**, **Delete**
* **Reload config** button re-reads `config.json`

Status is refreshed every ~3s.

---

## Authentication

Two options, both enabled if configured:

### 1) Basic auth

Set `auth.username` and `auth.password` (or set `PSM_USERNAME`/`PSM_PASSWORD`).
Example curl:

```bash
curl -u admin:changeme http://localhost:8088/api/status
```

### 2) Bearer token

Set `auth.token` or `PSM_TOKEN`:

```bash
curl -H "Authorization: Bearer MY_SUPER_TOKEN" http://localhost:8088/api/status
```

If no credentials or token are set at all, the server allows unauthenticated access.

---

## HTTP API

Base path under `http://<host>:8088`

| Method | Path                | Description                           |      |           |
| -----: | ------------------- | ------------------------------------- | ---- | --------- |
|    GET | `/`                 | Dashboard (HTML)                      |      |           |
|    GET | `/logs/{name}`      | Logs viewer (HTML)                    |      |           |
|    GET | `/api/status`       | JSON status for all sites             |      |           |
|    GET | `/api/logs/{name}`  | Plaintext tail (query: `lines=200`)   |      |           |
|   POST | `/api/reload`       | Reload `config.json`                  |      |           |
|   POST | `/api/sites`        | Add site (form fields below)          |      |           |
|   POST | `/api/sites/delete` | Delete site (form: `name`)            |      |           |
|   POST | `/action`           | Control site (form: `name`, `op=start | stop | restart`) |
|    GET | `/stream/{name}`    | SSE stream of new log lines           |      |           |

### Add site (form fields)

* `name` (str, required, unique, no spaces/slashes)
* `cwd` (str, required, must exist)
* `cmd` (str, required)
* `port` (int, optional)
* `log` (str, default `activity.log`)
* `autostart` (`true|false`, default `false`)
* `autorestart` (`true|false`, default `false`)
* `start_after_add` (`true|false`, default `false`)

Example:

```bash
curl -u admin:changeme -X POST http://localhost:8088/api/sites \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data-urlencode "name=MySite" \
  --data-urlencode "cwd=/home/pi/myapp" \
  --data-urlencode "cmd=/usr/bin/python3 app.py" \
  --data-urlencode "port=5000" \
  --data-urlencode "log=activity.log" \
  --data-urlencode "autostart=true" \
  --data-urlencode "autorestart=true" \
  --data-urlencode "start_after_add=true"
```

---

## Systemd service (optional)

Create `/etc/systemd/system/pisite-manager.service`:

```ini
[Unit]
Description=PiSite Manager
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/pi/PiSiteManager
Environment=PSM_USERNAME=admin
Environment=PSM_PASSWORD=changeme
# Environment=PSM_TOKEN=MY_SUPER_TOKEN
ExecStart=/home/pi/PiSiteManager/.venv/bin/uvicorn manager:app --host 0.0.0.0 --port 8088 --workers 1
Restart=on-failure
User=pi
Group=pi

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now pisite-manager
```

---

## Troubleshooting

* **401 Unauthorized**: Supply Basic or Bearer creds. Verify env vars or `auth` block.
* **"Session not found or tmux not available"**: Install tmux or use background mode (it will auto-fallback).

  ```bash
  sudo apt-get update && sudo apt-get install -y tmux
  ```
* **No logs**: Ensure your command prints to stdout/stderr. PiSite pipes output into the configured log file.
* **SSE blocked**: Some reverse proxies buffer SSE. Disable buffering for `/stream/*` or rely on polling fallback.

---

## Security notes

* Use **random, strong** passwords/tokens in production.
* Expose the service behind a reverse proxy with HTTPS (Caddy/Nginx/Traefik).
* Limit network access to trusted subnets if possible (firewall).

---

## License

MIT (or your preferred license). See `LICENSE`.
