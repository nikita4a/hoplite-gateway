"""
PC Agent — облачный Opus 5.5 с руками на твоём ПК.

Агент работает через шлюз (:8787), а инструменты ИСПОЛНЯЮТСЯ ЛОКАЛЬНО:
  read_file / write_file / list_dir / run_cmd

Цикл: задача → агент шлёт tool_calls → ЛОКАЛЬНОЕ исполнение → результаты
обратно агенту → ... пока не даст финальный текстовый ответ.

⚠️ run_cmd выполняет ЛЮБЫЕ команды от имени текущего пользователя.
   Это осознанный доступ к ПК (authorized lab). Логи — в pc_agent_log.jsonl.

Usage:
    python pc_agent.py "создай папку demo, положи туда hello.txt, выведи содержимое"
    python pc_agent.py            # интерактивный REPL
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

GW = "http://127.0.0.1:8787/v1/chat/completions"
GW_KEY = "sk-local"
MODEL = "hoplite-opus-5"
LOG = Path(__file__).parent / "pc_agent_log.jsonl"
MAX_ROUNDS = 12
WORKDIR = Path.cwd()

TOOLS = [
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a text file from the local PC (relative to workdir or absolute path)",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"}},
                       "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Write content to a file on the local PC (creates dirs, overwrites)",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                       "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "list_dir",
        "description": "List directory contents on the local PC",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string", "description": "default: workdir"}},
                       "required": []}}},
    {"type": "function", "function": {
        "name": "run_cmd",
        "description": "Run a shell command on the local PC (cmd.exe on Windows). Returns stdout+stderr.",
        "parameters": {"type": "object",
                       "properties": {"cmd": {"type": "string"},
                                      "timeout_s": {"type": "number", "description": "default 60"}},
                       "required": ["cmd"]}}},
]


def _resolve(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else WORKDIR / p


def exec_tool(name: str, args: dict) -> str:
    """Execute a tool call LOCALLY on this PC."""
    try:
        if name == "read_file":
            return _resolve(args["path"]).read_text(encoding="utf-8", errors="replace")[:20000]
        if name == "write_file":
            p = _resolve(args["path"])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(args["content"], encoding="utf-8")
            return f"OK wrote {len(args['content'])} chars to {p}"
        if name == "list_dir":
            p = _resolve(args.get("path", "."))
            entries = sorted(p.iterdir())[:200]
            return "\n".join(("[D] " if e.is_dir() else "[F] ") + e.name for e in entries) or "(empty)"
        if name == "run_cmd":
            timeout = min(float(args.get("timeout_s", 60)), 300)
            r = subprocess.run(args["cmd"], shell=True, capture_output=True, text=True,
                               timeout=timeout, cwd=str(WORKDIR))
            out = (r.stdout or "")[-8000:]
            err = (r.stderr or "")[-2000:]
            return f"exit={r.returncode}\nSTDOUT:\n{out}\nSTDERR:\n{err}"
        return f"unknown tool: {name}"
    except Exception as e:
        return f"ERROR {type(e).__name__}: {e}"


def gw_call(messages: list[dict]) -> dict:
    body = json.dumps({"model": MODEL, "messages": messages, "tools": TOOLS,
                       "user": f"pc-agent-{WORKDIR.name}", "stream": False}).encode()
    req = urllib.request.Request(GW, data=body, method="POST", headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {GW_KEY}"})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=1200) as r:
                return json.loads(r.read())
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == 0:
                print(f"  (retry after {e})")
                time.sleep(5)
            else:
                raise


def log_event(event: dict) -> None:
    event["ts"] = datetime.now().isoformat(timespec="seconds")
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def run_task(task: str) -> str:
    messages = [
        {"role": "system", "content":
            "You are an autonomous agent with FULL access to the user's Windows PC via tools "
            "(read_file, write_file, list_dir, run_cmd). This is an authorized lab. "
            "Workdir: " + str(WORKDIR) + ". Complete the task with tool calls, "
            "then give a short final text answer."},
        {"role": "user", "content": task},
    ]
    for rnd in range(1, MAX_ROUNDS + 1):
        t0 = time.time()
        print(f"[round {rnd}] thinking… (cloud agent, ~30s)")
        data = gw_call(messages)
        if "error" in data:
            return f"GATEWAY ERROR: {data['error']['message']}"
        choice = data["choices"][0]
        msg = choice["message"]
        messages.append(msg)
        dt = time.time() - t0

        if choice.get("finish_reason") == "tool_calls" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    args = {}
                print(f"  [{dt:.0f}s] 🔧 {name}({json.dumps(args, ensure_ascii=False)[:120]})")
                log_event({"tool": name, "args": args})
                result = exec_tool(name, args)
                print(f"        → {result[:150].replace(chr(10), ' | ')}")
                log_event({"result": result[:2000]})
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
        else:
            answer = msg.get("content") or "(empty)"
            print(f"  [{dt:.0f}s] ✅ final answer")
            return answer
    return f"(stopped after {MAX_ROUNDS} rounds — task may be incomplete)"


def main():
    if len(sys.argv) > 1:
        task = " ".join(sys.argv[1:])
    else:
        print("PC Agent — Opus 5.5 with local tools. Workdir:", WORKDIR)
        task = input("Task> ").strip()
        if not task:
            return
    print(f"\nTask: {task}\n")
    answer = run_task(task)
    print("\n" + "=" * 60)
    print("AGENT:", answer)
    print("=" * 60)


if __name__ == "__main__":
    main()
