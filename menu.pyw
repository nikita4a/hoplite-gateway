"""
Hoplite Gateway — desktop menu (Tkinter).
Double-click to run:  pythonw menu.pyw   (or python menu.pyw)

Controls the gateway: start/stop, API key, ngrok tunnel, dashboard, tests,
OMP/OpenCode config copying. No admin rights needed.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
LEGACY_FILE = BASE_DIR / "cookies.json"
HOST, PORT = "127.0.0.1", 8787
GW_URL = f"http://{HOST}:{PORT}"

BG, CARD, FG, MUT, ACC, OK, ERR = "#0d1117", "#161b22", "#e6edf3", "#8b949e", "#58a6ff", "#3fb950", "#f85149"


def _py() -> str:
    return sys.executable or "python"


def _http(path: str, data: dict | None = None, timeout: float = 8.0):
    req = urllib.request.Request(GW_URL + path, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json"})
    body = json.dumps(data).encode() if data else None
    try:
        with urllib.request.urlopen(req, body, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _load_cfg() -> dict:
    for p in (CONFIG_FILE, LEGACY_FILE):
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
    return {}


def _save_cfg(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def _port_busy() -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((HOST, PORT)) == 0


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Hoplite Gateway — Opus 5.5 as OpenAI API")
        root.geometry("640x620")
        root.configure(bg=BG)
        self.proc: subprocess.Popen | None = None
        self._build()
        self._refresh()

    # ── ui ────────────────────────────────────────────────────────────────
    def _build(self):
        pad = {"padx": 14, "pady": 6}
        tk.Label(self.root, text="⚡ Hoplite OpenAI Gateway", bg=BG, fg=FG,
                 font=("Segoe UI", 16, "bold")).pack(anchor="w", **pad)
        tk.Label(self.root, text="Cloud coding agents (Opus 5.5) → OpenAI v1 API for OMP / OpenCode / any client",
                 bg=BG, fg=MUT, font=("Segoe UI", 9)).pack(anchor="w", padx=14)

        # status card
        self.status_box = tk.Label(self.root, text="checking…", bg=CARD, fg=FG, justify="left",
                                   font=("Consolas", 10), anchor="w", padx=12, pady=10)
        self.status_box.pack(fill="x", **pad)

        # buttons
        grid = tk.Frame(self.root, bg=BG)
        grid.pack(fill="x", **pad)
        buttons = [
            ("▶  Start gateway", self.start, ACC, "#04121f"),
            ("■  Stop gateway", self.stop, "#21262d", FG),
            ("🌐  Open dashboard", lambda: webbrowser.open(GW_URL), "#21262d", FG),
            ("🔗  Start ngrok tunnel", self.ngrok_start, "#21262d", FG),
            ("✂  Stop ngrok", self.ngrok_stop, "#21262d", FG),
            ("🧪  Send test message", self.test_msg, "#21262d", FG),
            ("🔑  Set API key…", self.set_key, "#21262d", FG),
            ("📋  Copy OMP config", self.copy_omp, "#21262d", FG),
            ("📋  Copy OpenCode config", self.copy_opencode, "#21262d", FG),
            ("🚀  Handoff project → cloud", self.handoff, "#21262d", FG),
            ("📂  Open config.json", self.open_cfg, "#21262d", FG),
        ]
        for i, (text, cmd, bg, fg) in enumerate(buttons):
            b = tk.Button(grid, text=text, command=cmd, bg=bg, fg=fg, activebackground=bg,
                          activeforeground=fg, relief="flat", font=("Segoe UI", 10, "bold"),
                          cursor="hand2", padx=10, pady=7)
            b.grid(row=i // 2, column=i % 2, sticky="ew", padx=4, pady=3)
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)

        # log
        tk.Label(self.root, text="Log", bg=BG, fg=MUT, font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=14)
        self.log_box = tk.Text(self.root, bg=CARD, fg=FG, insertbackground=FG, relief="flat",
                               font=("Consolas", 9), height=12, state="disabled", wrap="word")
        self.log_box.pack(fill="both", expand=True, padx=14, pady=(2, 14))

    def log(self, msg: str):
        def _do():
            self.log_box.configure(state="normal")
            self.log_box.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg}\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        self.root.after(0, _do)

    # ── status ────────────────────────────────────────────────────────────
    def _refresh(self):
        h = _http("/health")
        cfg = _load_cfg()
        running = h is not None
        auth = bool(h and h.get("auth")) or bool(cfg.get("api_key"))
        ngrok_url = (h or {}).get("ngrok") or "off"
        color = OK if running else ERR
        self.status_box.configure(fg=color, text=(
            f"Gateway:  {'RUNNING' if running else 'STOPPED'}   http://{HOST}:{PORT}\n"
            f"Auth:     {'✓ API key' if auth else '✗ NO KEY — click Set API key'}\n"
            f"ngrok:    {ngrok_url}\n"
            f"Stats:    requests={h.get('requests', 0) if h else 0}  threads={h.get('threads_created', 0) if h else 0}"
            f"  errors={h.get('errors', 0) if h else 0}"))
        self.root.after(3000, self._refresh)

    # ── actions ───────────────────────────────────────────────────────────
    def start(self):
        if _port_busy():
            self.log("Port 8787 already busy — gateway seems running.")
            return
        cfg = _load_cfg()
        if not cfg.get("api_key"):
            self.log("No API key yet — set it first (🔑).")
            self.set_key()
            return
        flags = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
        self.proc = subprocess.Popen([_py(), str(BASE_DIR / "server.py")],
                                     cwd=str(BASE_DIR), creationflags=flags)
        self.log(f"Gateway starting (pid {self.proc.pid})… console window opened.")
        for _ in range(15):
            time.sleep(1)
            if _http("/health"):
                self.log("Gateway is UP → " + GW_URL)
                return
        self.log("Gateway did not answer in 15s — check its console window.")

    def stop(self):
        stopped = False
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            stopped = True
        # also kill whoever holds the port (started outside menu)
        if os.name == "nt":
            try:
                out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
                for line in out.splitlines():
                    if f":{PORT}" in line and "LISTENING" in line:
                        pid = line.split()[-1]
                        subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True)
                        stopped = True
            except Exception:
                pass
        self.log("Gateway stopped." if stopped else "Gateway was not running.")

    def _ngrok(self, action: str):
        r = _http("/admin/ngrok", {"action": action}, timeout=25)
        if r and r.get("url"):
            self.log(f"ngrok URL: {r['url']}")
            self.root.clipboard_clear()
            self.root.clipboard_append(r["url"] + "/v1")
            self.log("Copied to clipboard: " + r["url"] + "/v1")
        elif r and r.get("error"):
            self.log("ngrok error: " + r["error"]["message"])
        else:
            self.log("ngrok: gateway not responding — start it first.")

    def ngrok_start(self):
        threading.Thread(target=self._ngrok, args=("start",), daemon=True).start()
        self.log("Starting ngrok tunnel…")

    def ngrok_stop(self):
        self._ngrok("stop")

    def set_key(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("Hoplite API key")
        dlg.configure(bg=BG)
        dlg.geometry("520x230")
        dlg.transient(self.root)
        tk.Label(dlg, text="Paste your Hoplite API key (hop_…)\nGet one: app.hoplite.sh → Settings → API Keys",
                 bg=BG, fg=MUT, font=("Segoe UI", 10), justify="left").pack(anchor="w", padx=14, pady=(14, 6))
        ent = tk.Entry(dlg, bg=CARD, fg=FG, insertbackground=FG, relief="flat", font=("Consolas", 11))
        ent.pack(fill="x", padx=14, pady=4)
        ent.insert(0, _load_cfg().get("api_key", ""))
        pid_lbl = tk.Label(dlg, text="Project ID override (optional):", bg=BG, fg=MUT, font=("Segoe UI", 9))
        pid_lbl.pack(anchor="w", padx=14)
        pid = tk.Entry(dlg, bg=CARD, fg=FG, insertbackground=FG, relief="flat", font=("Consolas", 10))
        pid.pack(fill="x", padx=14, pady=4)
        pid.insert(0, _load_cfg().get("project_id", ""))

        def save():
            cfg = _load_cfg()
            cfg["api_key"] = ent.get().strip()
            cfg["api_base"] = cfg.get("api_base", "https://api.hoplite.sh")
            cfg["project_id"] = pid.get().strip()
            _save_cfg(cfg)
            _http("/admin/config", {"api_key": cfg["api_key"], "project_id": cfg["project_id"]})
            self.log("API key saved to config.json (and hot-applied if gateway is running).")
            dlg.destroy()
        tk.Button(dlg, text="Save", command=save, bg=ACC, fg="#04121f", relief="flat",
                  font=("Segoe UI", 10, "bold"), padx=16, pady=6).pack(pady=12)

    def test_msg(self):
        if not _http("/health"):
            self.log("Gateway not running — start it first.")
            return
        self.log("Test message sent to agent… (30–90s, real cloud agent)")

        def _run():
            try:
                req = urllib.request.Request(
                    GW_URL + "/v1/chat/completions",
                    data=json.dumps({"model": "hoplite-opus-5",
                                     "messages": [{"role": "user", "content": "Reply with exactly: GATEWAY OK"}]}).encode(),
                    headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=300) as r:
                    data = json.loads(r.read())
                txt = data["choices"][0]["message"]["content"]
                self.log("✅ Agent replied: " + txt[:200])
            except Exception as e:
                self.log(f"❌ Test failed: {e}")
        threading.Thread(target=_run, daemon=True).start()

    def copy_omp(self):
        h = _http("/health") or {}
        base = (h.get("ngrok") or f"http://{HOST}:{PORT}") + "/v1"
        snippet = (f"hermes-hoplite:\n  api: openai-completions\n  apiKey: sk-local\n"
                   f"  authHeader: true\n  baseUrl: {base}\n  compat:\n    maxTokensField: max_tokens\n"
                   f"    supportsStreaming: true\n    supportsToolChoice: true\n  models:\n"
                   f"    - id: hoplite-opus-5\n      name: hoplite-opus-5\n      contextWindow: 200000\n"
                   f"      maxTokens: 16384\n      input: [text]\n      reasoning: true\n"
                   f"      cost: {{input: 0, output: 0, cacheRead: 0, cacheWrite: 0}}")
        self.root.clipboard_clear()
        self.root.clipboard_append(snippet)
        self.log("OMP models.yml snippet copied to clipboard. Add under providers: in ~/.omp/agent/models.yml")

    def copy_opencode(self):
        h = _http("/health") or {}
        base = (h.get("ngrok") or f"http://{HOST}:{PORT}") + "/v1"
        snippet = json.dumps({"provider": {"hoplite": {"npm": "@ai-sdk/openai-compatible",
                                                       "options": {"baseURL": base, "apiKey": "sk-local"},
                                                       "models": {"hoplite-opus-5": {"name": "Hoplite Opus 5.5"}}}}},
                             indent=2)
        self.root.clipboard_clear()
        self.root.clipboard_append(snippet)
        self.log("OpenCode config copied to clipboard (goes into ~/.config/opencode/opencode.json).")

    def handoff(self):
        from tkinter import filedialog
        d = filedialog.askdirectory(title="Project directory to hand off (git repo with GitHub remote)")
        if not d:
            return
        exe = "hoplite"
        cand = Path.home() / "AppData/Local/Hoplite/bin/hoplite.exe"
        if cand.exists():
            exe = str(cand)
        self.log(f"Handoff {d} → Hoplite cloud (autopush, opencode session)…")

        def _run():
            try:
                flags = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
                subprocess.Popen([exe, "handoff", "--cwd", d, "--autopush", "--harness", "opencode"],
                                 creationflags=flags)
                self.log("Handoff console opened — follow prompts there.")
            except Exception as e:
                self.log(f"handoff failed: {e}")
        threading.Thread(target=_run, daemon=True).start()

    def open_cfg(self):
        if not CONFIG_FILE.exists():
            _save_cfg({"api_key": _load_cfg().get("api_key", ""), "api_base": "https://api.hoplite.sh",
                       "project_id": "", "ngrok_token": ""})
        if os.name == "nt":
            os.startfile(str(CONFIG_FILE))  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", str(CONFIG_FILE)])


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
