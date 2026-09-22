"""
Hoplite OpenAI Gateway v3 — hardened.

Drop your Hoplite API key into config.json → get an OpenAI v1 compatible
endpoint backed by Hoplite cloud agents (Claude Opus 5.5 etc).

Fixes over v2:
  - conversation continuity (thread reuse per conversation, message append)
  - REAL streaming (polls assistant deltas while agent works)
  - tool-calling protocol (prompt-based JSON tool_calls, parsed back to OpenAI format)
  - project cache, concurrency semaphore, mapped errors, CORS
  - built-in web dashboard at /
  - admin endpoints for hot config + ngrok control
  - NO secrets in repo (config.json gitignored)

Run:  python server.py     → http://127.0.0.1:8787
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

# ── paths / constants ──────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
LEGACY_CONFIG = BASE_DIR / "cookies.json"   # backward compat
HOST, PORT = "127.0.0.1", 8787

MAX_CONCURRENT = 3          # parallel Hoplite threads
POLL_FAST, POLL_SLOW = 1.0, 2.5   # seconds between message polls
DEADLINE = 540              # default; override with "deadline_s" in config.json
PROJECT_TTL = 300           # seconds to cache project list

# gateway model → Hoplite `model` field (probed against the live API 2026-09).
# None = Hoplite project default. "-fast" suffix on any name → speed:fast.
MODEL_ID_MAP = {
    "hoplite-opus-5":       "claude-opus-5",     # Claude Opus 5.5 generation
    "hoplite-sonnet-5":     "claude-sonnet-5",
    "hoplite-opus-4.8":     "claude-opus-4-8",
    "hoplite-gpt-5.5":      "gpt-5.5",
    "hoplite-gpt-5.6-terra": "gpt-5.6-terra",
    "hoplite-agent":        None,
    "hoplite-agent-stream": None,
    "hoplite-code":         None,
    "hoplite-review":       None,
    "hoplite-plan":         None,
}

MODELS = list(MODEL_ID_MAP.keys())

REASONING_MODES = {"off", "none", "minimal", "low", "medium", "high", "xhigh", "max"}

TERMINAL_STATUSES = {"completed", "failed", "cancelled", "canceled", "stopped", "error"}

# ── config ─────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    if LEGACY_CONFIG.exists():   # migrate cookies.json → config.json
        old = json.loads(LEGACY_CONFIG.read_text(encoding="utf-8"))
        cfg = {"api_key": old.get("api_key", ""), "api_base": old.get("api_base", "https://api.hoplite.sh")}
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        return cfg
    return {}

def _save_config(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

CONFIG = _load_config()
API_KEY: str = CONFIG.get("api_key", "")
API_BASE: str = CONFIG.get("api_base", "https://api.hoplite.sh")
PROJECT_ID_OVERRIDE: str = CONFIG.get("project_id", "")
DEADLINE = int(CONFIG.get("deadline_s", DEADLINE))

# ── runtime state ──────────────────────────────────────────────────────────

_sem = asyncio.Semaphore(MAX_CONCURRENT)
_project_cache: dict[str, Any] = {"projects": None, "ts": 0.0}
_conv_threads: dict[str, str] = {}       # conv key → hoplite thread id
_ngrok_proc: subprocess.Popen | None = None
_ngrok_url: str = ""
_stats = {"requests": 0, "threads_created": 0, "errors": 0, "started": time.time()}

# ── http helpers ───────────────────────────────────────────────────────────

def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=API_BASE,
        headers={"X-Api-Key": API_KEY, "Accept": "application/json",
                 "User-Agent": "HopliteOpenAIGateway/3.0"},
        timeout=httpx.Timeout(60.0, connect=10.0),
    )

class HopliteError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status

async def _api(client: httpx.AsyncClient, method: str, path: str,
               retries: int = 2, **kw) -> Any:
    """Hoplite API call with backoff on 429/5xx. Raises HopliteError on hard failures."""
    delay = 5.0
    for attempt in range(retries + 1):
        r = await client.request(method, path, **kw)
        if r.status_code in (401, 403):
            raise HopliteError(f"auth failed ({r.status_code}): bad/expired api_key or missing scope. "
                               f"Get a new key at https://app.hoplite.sh → Settings → API Keys", 401)
        if r.status_code == 429 or r.status_code >= 500:
            if attempt < retries:
                ra = float(r.headers.get("retry-after") or delay)
                await asyncio.sleep(min(ra, 30.0))
                delay *= 2
                continue
            raise HopliteError(f"Hoplite {r.status_code} after {retries+1} attempts — account rate "
                               f"limit; wait a few minutes (too many threads today?)", r.status_code)
        if r.status_code >= 400:
            raise HopliteError(f"Hoplite API {r.status_code}: {r.text[:200]}", r.status_code)
        return r.json()
    raise HopliteError("unreachable", 500)

async def _get_project(client: httpx.AsyncClient) -> dict:
    if PROJECT_ID_OVERRIDE:
        return {"id": PROJECT_ID_OVERRIDE, "name": PROJECT_ID_OVERRIDE}
    now = time.time()
    if _project_cache["projects"] and now - _project_cache["ts"] < PROJECT_TTL:
        projects = _project_cache["projects"]
    else:
        data = await _api(client, "GET", "/api/projects")
        projects = data.get("projects") or data.get("data") or []
        _project_cache.update(projects=projects, ts=now)
    if not projects:
        raise HopliteError("no Hoplite projects found — create one at https://app.hoplite.sh", 404)
    return projects[0]

# ── hoplite thread operations ──────────────────────────────────────────────

async def _create_thread(client: httpx.AsyncClient, pid: str, prompt: str,
                         model: str, reasoning: str | None = None) -> str:
    """Create a Hoplite thread. `model` is the gateway model name; mapped to the
    probed-valid Hoplite `model` field. Unknown/invalid → project default."""
    base = model[:-5] if model.endswith("-fast") else model
    fast = model.endswith("-fast")
    body: dict[str, Any] = {"projectId": pid, "prompt": prompt}
    if fast:
        body["speed"] = "fast"
    mid = MODEL_ID_MAP.get(base, "unknown") if base in MODEL_ID_MAP else None
    if mid:
        body["model"] = mid
    if reasoning and reasoning.lower() in REASONING_MODES:
        body["reasoning"] = {"mode": reasoning.lower()}
    try:
        data = await _api(client, "POST", "/api/threads", json=body, retries=1)
    except HopliteError:
        body.pop("model", None)   # invalid model → retry on project default
        body.pop("reasoning", None)
        data = await _api(client, "POST", "/api/threads", json=body)
    _stats["threads_created"] += 1
    return data["thread"]["id"]

async def _append_message(client: httpx.AsyncClient, tid: str, text: str) -> None:
    await _api(client, "POST", f"/api/threads/{tid}/messages", json={"content": text})

async def _msg_list(client: httpx.AsyncClient, tid: str) -> list[dict]:
    data = await _api(client, "GET", f"/api/threads/{tid}/messages")
    if isinstance(data, list):
        return data
    return data.get("messages") or data.get("data") or []

async def _thread_status(client: httpx.AsyncClient, tid: str) -> str:
    data = await _api(client, "GET", f"/api/threads/{tid}")
    t = data.get("thread", data)
    return (t.get("status") or "").lower()

def _latest_assistant(msgs: list[dict]) -> str:
    """Concatenate assistant text that came after the last user message."""
    out: list[str] = []
    for m in reversed(msgs):
        role = m.get("role")
        if role == "user":
            break
        if role == "assistant" and m.get("content"):
            out.append(m["content"])
    return "\n".join(reversed(out)).strip()

async def _run_agent(client: httpx.AsyncClient, tid: str,
                     on_delta=None) -> str:
    """Poll thread until the agent finishes. Streams deltas via on_delta(text_so_far)."""
    start = time.time()
    sent_len = 0
    interval = POLL_FAST
    while time.time() - start < DEADLINE:
        await asyncio.sleep(interval)
        interval = POLL_FAST if time.time() - start < 15 else POLL_SLOW
        try:
            msgs = await _msg_list(client, tid)
            text = _latest_assistant(msgs)
            if on_delta and len(text) > sent_len:
                await on_delta(text[sent_len:])
                sent_len = len(text)
            status = await _thread_status(client, tid)
            # Hoplite turn lifecycle: queued → running → ready (idle, turn done).
            # "ready" + assistant text = the agent finished this turn.
            if status in ("failed", "error", "cancelled", "canceled", "stopped") and not text:
                raise HopliteError(f"agent thread {tid} ended with status '{status}' "
                                   f"— see https://app.hoplite.sh", 502)
            if text and status in ("ready", "completed", "idle", "waiting"):
                # final fetch — status may flip before last chunk lands
                msgs = await _msg_list(client, tid)
                text = _latest_assistant(msgs)
                if on_delta and len(text) > sent_len:
                    await on_delta(text[sent_len:])
                return text or f"[thread {tid} finished with no text output]"
        except HopliteError as e:
            if e.status in (401, 403):
                raise  # auth is fatal
            # 429/5xx during polling — transient, back off and keep waiting
            await asyncio.sleep(5.0)
        except Exception:
            pass  # transient poll errors — keep trying until deadline
    raise HopliteError(f"agent did not finish within {DEADLINE}s (thread {tid}) — it may still complete, see https://app.hoplite.sh", 504)

# ── conversation mapping ───────────────────────────────────────────────────

def _conv_key(messages: list[dict], user: str | None) -> str:
    if user:
        return f"user:{user}"
    first = next((m for m in messages if m.get("role") == "user"), None)
    seed = json.dumps(first.get("content", ""), ensure_ascii=False)[:256] if first else "empty"
    return "h:" + hashlib.md5(seed.encode()).hexdigest()[:16]

def _render_history(messages: list[dict]) -> str:
    """Compact transcript for thread creation (Hoplite threads start fresh)."""
    lines = []
    for m in messages[:-1][-8:]:
        role, content = m.get("role", "?"), m.get("content", "")
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        if role == "tool":
            content = f"[tool result] {content}"
        lines.append(f"{role}: {str(content)[:1500]}")
    return "\n".join(lines)

def _last_user_text(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            c = m.get("content", "")
            if isinstance(c, list):
                c = " ".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
            return str(c)
    return ""

def _tool_results_text(messages: list[dict]) -> str:
    """Format trailing role=tool messages (client answered our tool_calls)."""
    out = []
    for m in reversed(messages):
        if m.get("role") != "tool":
            break
        out.append(f"Tool result: {str(m.get('content', ''))[:3000]}")
    return "\n".join(reversed(out))

# ── tool-calling protocol (prompt-based) ───────────────────────────────────

_TOOL_INSTRUCTIONS = """

---
SYSTEM: You are acting as an OpenAI-compatible tool-calling backend.
Available tools (JSON schemas):
{tools}

RULES:
- If you need a tool, reply with ONLY raw JSON (no markdown, no prose):
  {{"tool_calls": [{{"name": "<tool>", "arguments": {{...}}}}]}}
- Multiple parallel calls allowed in the array.
- If no tool is needed, reply with plain text (the final answer).
- When you receive "Tool result:" messages, continue the task.
"""

def _wrap_tools(prompt: str, tools: list[dict]) -> str:
    slim = [{"name": t["function"]["name"],
             "description": t["function"].get("description", ""),
             "parameters": t["function"].get("parameters", {})} for t in tools
            if t.get("type") == "function" and "function" in t]
    if not slim:
        return prompt
    return prompt + _TOOL_INSTRUCTIONS.format(tools=json.dumps(slim, ensure_ascii=False)[:6000])

def _parse_tool_calls(text: str) -> list[dict] | None:
    """Detect the JSON tool_calls protocol in agent output."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.M).strip()
    m = re.search(r'\{.*"tool_calls".*\}', t, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    calls = data.get("tool_calls")
    if not isinstance(calls, list) or not calls:
        return None
    out = []
    for c in calls:
        name = c.get("name") or (c.get("function") or {}).get("name")
        args = c.get("arguments")
        if args is None:
            args = (c.get("function") or {}).get("arguments", {})
        if not name:
            continue
        out.append({
            "id": f"call_{uuid.uuid4().hex[:12]}",
            "type": "function",
            "function": {"name": name,
                         "arguments": json.dumps(args, ensure_ascii=False) if not isinstance(args, str) else args},
        })
    return out or None

# ── OpenAI response builders ───────────────────────────────────────────────

def _chat_response(cid: str, model: str, content: str,
                   tool_calls: list[dict] | None = None) -> dict:
    msg: dict[str, Any] = {"role": "assistant", "content": content or None}
    finish = "stop"
    if tool_calls:
        msg["tool_calls"] = tool_calls
        msg["content"] = content or None
        finish = "tool_calls"
    return {
        "id": cid, "object": "chat.completion", "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }

def _err(message: str, etype: str = "gateway_error", status: int = 502) -> JSONResponse:
    _stats["errors"] += 1
    return JSONResponse(status_code=status,
                        content={"error": {"message": message, "type": etype, "code": etype}})

def _sse(cid: str, model: str, delta: dict, finish: str | None = None) -> str:
    chunk = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
             "model": model, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

# ── core completion ────────────────────────────────────────────────────────

async def _complete(body: dict) -> Any:
    global API_KEY, API_BASE
    model = body.get("model", "hoplite-agent")
    messages = body.get("messages") or []
    tools = body.get("tools")
    stream = bool(body.get("stream"))
    cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    _stats["requests"] += 1

    if not API_KEY:
        msg = ("No API key. Open http://127.0.0.1:8787/ (dashboard) or put "
               '{"api_key": "hop_..."} into config.json next to server.py, then restart.')
        return _stream_or_static(stream, cid, model, msg, None, _err(msg, "auth_error", 401))

    user_text = _last_user_text(messages)
    tool_results = _tool_results_text(messages)
    conv = _conv_key(messages, body.get("user"))
    history = _render_history(messages)

    prompt_parts = []
    if history:
        prompt_parts.append(f"[earlier conversation]\n{history}")
    if tool_results:
        prompt_parts.append(tool_results)
    if user_text and not tool_results:
        prompt_parts.append(user_text)
    prompt = "\n\n".join(prompt_parts) or "Hello — introduce yourself briefly."
    if tools:
        prompt = _wrap_tools(prompt, tools)

    async with _sem:
        try:
            async with _client() as c:
                project = await _get_project(c)
                tid = _conv_threads.get(conv)
                if tid:
                    try:
                        await _append_message(c, tid, prompt)
                    except Exception as ae:
                        print(f"[gw] append to {tid} failed ({ae}) — recreating thread with history",
                              file=sys.stderr, flush=True)
                        tid = None  # thread gone/unusable → recreate with serialized history
                if not tid:
                    tid = await _create_thread(c, project["id"], prompt, model,
                                               reasoning=body.get("reasoning_effort"))
                    _conv_threads[conv] = tid

                if stream:
                    return StreamingResponse(
                        _stream_run(c, cid, model, tid, tools),
                        media_type="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

                text = await _run_agent(c, tid)
                calls = _parse_tool_calls(text) if tools else None
                if calls:
                    text = ""
                return JSONResponse(_chat_response(cid, model, text, calls))
        except HopliteError as e:
            return _err(str(e), "gateway_error", e.status)
        except httpx.HTTPError as e:
            return _err(f"network error talking to Hoplite: {e}", "gateway_error", 502)
        except Exception as e:
            return _err(f"gateway bug: {type(e).__name__}: {e}", "gateway_error", 500)

async def _stream_run(c: httpx.AsyncClient, cid: str, model: str,
                      tid: str, tools) -> AsyncGenerator[str, None]:
    """Real streaming: deltas as the agent writes; tool_calls emitted at end."""
    yield _sse(cid, model, {"role": "assistant"})
    buffer = ""
    try:
        async def on_delta(piece: str):
            nonlocal buffer
            buffer += piece
        # NOTE: _run_agent awaits on_delta; we collect then flush per poll tick.
        # To stream incrementally we wrap: emit inside on_delta via queue.
        q: asyncio.Queue[str | None] = asyncio.Queue()

        async def on_delta_q(piece: str):
            await q.put(piece)

        task = asyncio.create_task(_run_agent(c, tid, on_delta_q))
        last_emit = time.time()
        while True:
            try:
                piece = await asyncio.wait_for(q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                if task.done():
                    break
                if time.time() - last_emit > 15:
                    last_emit = time.time()
                    yield ": hb\n\n"   # SSE comment heartbeat — keeps idle connections alive
                continue
            last_emit = time.time()
            if piece:
                yield _sse(cid, model, {"content": piece})
            if task.done() and q.empty():
                break
        text = await task
        calls = _parse_tool_calls(text) if tools else None
        if calls:
            yield _sse(cid, model, {"tool_calls": [
                {"index": i, "id": tc["id"], "type": "function", "function": tc["function"]}
                for i, tc in enumerate(calls)]})
            yield _sse(cid, model, {}, "tool_calls")
        else:
            # flush any remainder not yet emitted (final fetch chunk)
            if len(text) > len(buffer):
                yield _sse(cid, model, {"content": text[len(buffer):]})
            yield _sse(cid, model, {}, "stop")
    except HopliteError as e:
        yield _sse(cid, model, {"content": f"\n[gateway error] {e}"}, "stop")
    except Exception as e:
        yield _sse(cid, model, {"content": f"\n[gateway error] {type(e).__name__}: {e}"}, "stop")
    yield "data: [DONE]\n\n"

# ── app ────────────────────────────────────────────────────────────────────

app = FastAPI(title="Hoplite OpenAI Gateway", version="3.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/v1/models")
async def v1_models():
    now = int(time.time())
    ids = list(MODELS) + [m + "-fast" for m in MODELS if not m.endswith("-stream")]
    return {"object": "list",
            "data": [{"id": m, "object": "model", "created": now, "owned_by": "hoplite"} for m in ids]}

@app.post("/v1/chat/completions")
async def v1_chat(request: Request):
    body = await request.json()
    result = await _complete(body)
    if isinstance(result, StreamingResponse):
        return result
    return result

@app.get("/health")
async def health():
    return {"ok": True, "auth": bool(API_KEY), "api_base": API_BASE,
            "ngrok": _ngrok_url or None, "uptime_s": int(time.time() - _stats["started"]),
            "active_conversations": len(_conv_threads), **{k: v for k, v in _stats.items() if k != "started"}}

@app.get("/setup")
async def setup():
    return {
        "howto": ["1. Get API key: https://app.hoplite.sh → Settings → API Keys",
                  "2. POST {\"api_key\": \"hop_...\"} to /admin/config (or edit config.json)",
                  "3. Use OpenAI clients: base http://127.0.0.1:8787/v1, any api key, model hoplite-opus-5"],
        "endpoints": {"models": "/v1/models", "chat": "/v1/chat/completions",
                      "dashboard": "/", "health": "/health",
                      "admin_config": "POST /admin/config", "admin_ngrok": "POST /admin/ngrok"},
    }

@app.post("/admin/config")
async def admin_config(request: Request):
    global API_KEY, API_BASE, PROJECT_ID_OVERRIDE
    body = await request.json()
    if "api_key" in body:
        API_KEY = str(body["api_key"]).strip()
    if "api_base" in body:
        API_BASE = str(body["api_base"]).strip() or "https://api.hoplite.sh"
    if "project_id" in body:
        PROJECT_ID_OVERRIDE = str(body["project_id"]).strip()
    _save_config({"api_key": API_KEY, "api_base": API_BASE, "project_id": PROJECT_ID_OVERRIDE,
                  "ngrok_token": CONFIG.get("ngrok_token", "")})
    _project_cache.update(projects=None, ts=0)
    return {"ok": True, "auth": bool(API_KEY)}

@app.post("/admin/ngrok")
async def admin_ngrok(request: Request):
    """{"action": "start"|"stop"|"status"} — manages an ngrok tunnel to this gateway."""
    global _ngrok_proc, _ngrok_url
    body = await request.json()
    action = body.get("action", "status")
    if action == "status":
        return {"running": bool(_ngrok_proc and _ngrok_proc.poll() is None), "url": _ngrok_url or None}
    if action == "stop":
        if _ngrok_proc and _ngrok_proc.poll() is None:
            _ngrok_proc.terminate()
        _ngrok_proc, _ngrok_url = None, ""
        return {"ok": True, "running": False}
    # start
    if _ngrok_proc and _ngrok_proc.poll() is None:
        return {"ok": True, "running": True, "url": _ngrok_url}
    token = (body.get("token") or CONFIG.get("ngrok_token") or "").strip()
    try:
        args = ["ngrok", "http", str(PORT), "--log", "stdout"]
        if token:
            args += ["--authtoken", token]
        _ngrok_proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except FileNotFoundError:
        return _err("ngrok not installed — winget install ngrok.ngrok (or download from ngrok.com)",
                    "config_error", 400)
    for _ in range(15):  # wait for local API
        await asyncio.sleep(1)
        try:
            async with httpx.AsyncClient(timeout=3) as c:
                r = await c.get("http://127.0.0.1:4040/api/tunnels")
                tunnels = r.json().get("tunnels", [])
                if tunnels:
                    _ngrok_url = tunnels[0]["public_url"]
                    return {"ok": True, "running": True, "url": _ngrok_url}
        except Exception:
            pass
        if _ngrok_proc.poll() is not None:
            return _err("ngrok exited immediately — check token/quota", "config_error", 400)
    return _err("ngrok started but no tunnel appeared in 15s", "config_error", 500)

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(DASHBOARD_HTML)

DASHBOARD_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hoplite Gateway</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--bd:#30363d;--tx:#e6edf3;--mut:#8b949e;--acc:#58a6ff;--ok:#3fb950;--err:#f85149}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 -apple-system,'Segoe UI',sans-serif;background:var(--bg);color:var(--tx);padding:24px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:0 0 12px;color:var(--mut);font-weight:600}
.wrap{max-width:860px;margin:0 auto}.card{background:var(--card);border:1px solid var(--bd);border-radius:8px;padding:16px;margin-bottom:16px}
.row{display:flex;gap:16px;flex-wrap:wrap}.row>.card{flex:1;min-width:260px}
.pill{display:inline-block;padding:2px 10px;border-radius:12px;font-size:12px;font-weight:600}
.ok{background:#12261e;color:var(--ok)}.bad{background:#2d1618;color:var(--err)}
input,textarea{width:100%;padding:8px 10px;background:#0d1117;border:1px solid var(--bd);border-radius:6px;color:var(--tx);font:inherit}
button{padding:8px 14px;background:var(--acc);border:0;border-radius:6px;color:#04121f;font-weight:700;cursor:pointer;font-size:13px}
button.sec{background:#21262d;color:var(--tx);border:1px solid var(--bd)}
pre{background:#0d1117;border:1px solid var(--bd);border-radius:6px;padding:10px;overflow-x:auto;font-size:12px;white-space:pre-wrap;word-break:break-all}
#chatlog{max-height:300px;overflow-y:auto;margin-bottom:8px}
.msg{margin:6px 0;padding:8px 10px;border-radius:6px}.msg.user{background:#1c2a3a}.msg.bot{background:#16231a}
.small{color:var(--mut);font-size:12px}label{display:block;margin:8px 0 4px;color:var(--mut);font-size:12px}
</style></head><body><div class="wrap">
<h1>⚡ Hoplite OpenAI Gateway</h1>
<p class="small">Cloud coding agents (Opus 5.5) as an OpenAI v1 API</p>
<div class="row">
 <div class="card"><h2>Status</h2><div id="status">loading…</div></div>
 <div class="card"><h2>Config</h2>
  <label>Hoplite API key (hop_…)</label><input id="key" type="password" placeholder="hop_...">
  <label>Project ID (optional override)</label><input id="pid" placeholder="proj_... (blank = first project)">
  <div style="margin-top:10px;display:flex;gap:8px">
   <button onclick="saveCfg()">Save</button>
   <button class="sec" onclick="ngrok('start')">Start ngrok</button>
   <button class="sec" onclick="ngrok('stop')">Stop</button>
  </div>
 </div>
</div>
<div class="card"><h2>Test chat</h2>
 <div id="chatlog"></div>
 <div style="display:flex;gap:8px"><input id="q" placeholder="Ask the agent… (slow: 30-90s, it's a real cloud agent)" onkeydown="if(event.key==='Enter')send()">
 <button onclick="send()">Send</button></div>
</div>
<div class="card"><h2>Connect</h2><div id="connect"></div></div>
</div>
<script>
async function refresh(){
 try{const s=await(await fetch('/health')).json();
  const st=document.getElementById('status');
  st.innerHTML=`Auth: <span class="pill ${s.auth?'ok':'bad'}">${s.auth?'API key set':'NO KEY'}</span><br>
   ngrok: <span class="pill ${s.ngrok?'ok':'bad'}">${s.ngrok||'off'}</span><br>
   <span class="small">requests ${s.requests} · threads ${s.threads_created} · errors ${s.errors} · uptime ${s.uptime_s}s · convs ${s.active_conversations}</span>`;
  const base=(s.ngrok||location.origin)+'/v1';
  document.getElementById('connect').innerHTML=`<pre>OpenAI-compatible base URL:  ${base}
API key:                     anything (e.g. sk-local)
Model:                       hoplite-opus-5

# OMP CLI (~/.omp/agent/models.yml)
hermes-hoplite:
  api: openai-completions
  apiKey: sk-local
  baseUrl: ${base}

# curl test
curl ${base}/chat/completions -H 'Content-Type: application/json' \\
  -d '{"model":"hoplite-opus-5","messages":[{"role":"user","content":"hi"}]}'</pre>`;
 }catch(e){document.getElementById('status').innerHTML='<span class="pill bad">gateway down</span>'}
}
async function saveCfg(){
 await fetch('/admin/config',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({api_key:document.getElementById('key').value,project_id:document.getElementById('pid').value})});
 document.getElementById('key').value='';refresh();
}
async function ngrok(a){const r=await(await fetch('/admin/ngrok',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:a})})).json();
 if(r.url)prompt('ngrok URL:',r.url); if(r.error)alert(r.error.message); refresh();}
function log(who,t){const d=document.getElementById('chatlog');d.innerHTML+=`<div class="msg ${who}">${t.replace(/</g,'&lt;')}</div>`;d.scrollTop=d.scrollHeight}
async function send(){
 const q=document.getElementById('q');if(!q.value.trim())return;
 log('user',q.value);const msg=q.value;q.value='';log('bot','⏳ agent working (30-90s)…');
 try{const r=await(await fetch('/v1/chat/completions',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({model:'hoplite-opus-5',messages:[{role:'user',content:msg}]})})).json();
  const el=document.querySelector('#chatlog .msg.bot:last-child');
  el.textContent=r.choices?r.choices[0].message.content:('❌ '+(r.error?r.error.message:JSON.stringify(r)));
 }catch(e){log('bot','❌ '+e)}
}
refresh();setInterval(refresh,5000);
</script></body></html>"""

# ── main ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 56)
    print("  Hoplite OpenAI Gateway v3")
    print("=" * 56)
    print(f"  Local:     http://{HOST}:{PORT}")
    print(f"  Dashboard: http://{HOST}:{PORT}/")
    print(f"  OpenAI:    http://{HOST}:{PORT}/v1  (model: hoplite-opus-5)")
    print(f"  Auth:      {'API key ✓' if API_KEY else 'MISSING — open dashboard and paste key'}")
    print(f"  API base:  {API_BASE}")
    print("=" * 56)
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
