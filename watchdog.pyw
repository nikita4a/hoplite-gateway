"""
Hoplite Gateway Watchdog — keeps everything alive and reports status.

Every 30s:   health-check the gateway (:8787); revive if dead.
Every 45m:   refresh the Hoplite MCP OAuth token (~1h lifetime) and
             hot-patch ~/.omp/agent/mcp.json.
Every check: write status.json (machine-readable) + watchdog.log (human).

Run hidden:  pythonw watchdog.pyw      (autostart installs it at logon)
Single instance: second copy exits immediately (lockfile + pid check).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
LOCK = BASE / "watchdog.lock"
STATUS = BASE / "status.json"
LOG = BASE / "watchdog.log"
TOKEN_FILE = Path.home() / ".config/hoplite/mcp-oauth.json"
MCP_JSON = Path.home() / ".omp/agent/mcp.json"
HOPLITE_EXE = Path.home() / "AppData/Local/Hoplite/bin/hoplite.exe"

GW_URL = "http://127.0.0.1:8787/health"
CHECK_EVERY = 30          # seconds
MCP_REFRESH_EVERY = 45 * 60
FAILS_BEFORE_REVIVE = 3

_state = {"restarts": 0, "started": time.time(), "mcp_refreshes": 0, "mcp_last": None,
          "mcp_expires": None, "gateway_pid": None}


def log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    try:
        if sys.stdout:
            print(line, flush=True)
    except Exception:
        pass
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        if LOG.stat().st_size > 512 * 1024:          # keep log bounded
            tail = LOG.read_text(encoding="utf-8").splitlines()[-500:]
            LOG.write_text("\n".join(tail), encoding="utf-8")
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    """Windows-safe liveness check (os.kill is destructive on Windows)."""
    try:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        h = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return bool(ok) and code.value == STILL_ACTIVE
    except Exception:
        return False


def acquire_lock() -> bool:
    """Single instance: stale locks (dead pid) are taken over."""
    if LOCK.exists():
        try:
            pid = int(LOCK.read_text().strip())
            if pid != os.getpid() and _pid_alive(pid):
                return False         # another watchdog is alive
        except ValueError:
            pass
    LOCK.write_text(str(os.getpid()))
    return True


def release_lock() -> None:
    try:
        if LOCK.exists() and LOCK.read_text().strip() == str(os.getpid()):
            LOCK.unlink()
    except OSError:
        pass


def gateway_health() -> dict | None:
    try:
        with urllib.request.urlopen(GW_URL, timeout=5) as r:
            return json.loads(r.read())
    except Exception:
        return None


def start_gateway() -> subprocess.Popen:
    py = sys.executable.replace("pythonw.exe", "python.exe")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    p = subprocess.Popen([py, str(BASE / "server.py")], cwd=str(BASE),
                         creationflags=flags,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _state["gateway_pid"] = p.pid
    log(f"gateway started (pid {p.pid})")
    return p


def refresh_mcp_token() -> None:
    """hoplite mcp start → new OAuth token → patch OMP mcp.json."""
    exe = str(HOPLITE_EXE) if HOPLITE_EXE.exists() else "hoplite"
    try:
        subprocess.run([exe, "mcp", "start"], capture_output=True, timeout=120,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        tok = json.loads(TOKEN_FILE.read_text())
        access, expires = tok["accessToken"], tok.get("expiresAt")
        mcp = json.loads(MCP_JSON.read_text(encoding="utf-8"))
        entry = {"type": "http", "url": "https://api.hoplite.sh/mcp",
                 "headers": {"Authorization": f"Bearer {access}",
                             "Accept": "application/json, text/event-stream"}}
        (mcp.setdefault("mcpServers", mcp))["hoplite"] = entry
        MCP_JSON.write_text(json.dumps(mcp, indent=2, ensure_ascii=False), encoding="utf-8")
        _state.update(mcp_refreshes=_state["mcp_refreshes"] + 1,
                      mcp_last=datetime.now().isoformat(timespec="seconds"),
                      mcp_expires=expires)
        log(f"MCP token refreshed (expires {expires})")
    except Exception as e:
        log(f"MCP refresh failed (non-fatal): {type(e).__name__}: {e}")


def write_status(gw: dict | None) -> None:
    payload = {
        "gateway": "UP" if gw else "DOWN",
        "gateway_pid": _state["gateway_pid"],
        "gateway_auth": bool(gw and gw.get("auth")),
        "gateway_stats": {k: gw.get(k) for k in ("requests", "threads_created", "errors")} if gw else None,
        "watchdog_restarts": _state["restarts"],
        "watchdog_uptime_s": int(time.time() - _state["started"]),
        "mcp_token_expires": _state["mcp_expires"],
        "mcp_refreshes": _state["mcp_refreshes"],
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        STATUS.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        pass


def main() -> None:
    if not acquire_lock():
        sys.exit(0)                    # another watchdog already runs
    log("watchdog started")
    proc: subprocess.Popen | None = None
    fails = 0
    last_mcp = 0.0                     # refresh on first loop too

    try:
        while True:
            gw = gateway_health()
            if gw:
                fails = 0
                if proc is None:
                    # gateway is up but not ours (started by hub/user) — adopt it
                    _state["gateway_pid"] = "external"
            else:
                fails += 1
                if proc is not None and proc.poll() is not None:
                    log(f"gateway process exited (code {proc.returncode})")
                    proc = None
                if fails >= FAILS_BEFORE_REVIVE:
                    if proc is not None:
                        proc.terminate()
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                    proc = start_gateway()
                    _state["restarts"] += 1
                    fails = 0
                    time.sleep(5)      # give it a moment before next health check

            now = time.time()
            if now - last_mcp > MCP_REFRESH_EVERY:
                refresh_mcp_token()
                last_mcp = now

            write_status(gw)
            time.sleep(CHECK_EVERY)
    finally:
        release_lock()


if __name__ == "__main__":
    main()
