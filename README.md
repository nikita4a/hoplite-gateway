# ⚡ Hoplite OpenAI Gateway

**Drop your Hoplite API key → get Claude Opus 5.5 as an OpenAI-compatible API.**

Hoplite (https://hoplite.sh) runs cloud coding agents powered by top models
(Opus 5.5, Sonnet 5, GPT-5.x). This gateway wraps that into a standard
`/v1/chat/completions` endpoint that **OMP, OpenCode, LiteLLM, Cursor or any
OpenAI client** can consume.

```
Your client ──OpenAI v1──▶ Gateway (:8787) ──REST──▶ Hoplite cloud agent (Opus 5.5)
                           ▲ dashboard :8787/
                           ▲ ngrok tunnel (optional, one click)
```

---

## Quick start (Windows)

```
1. Double-click  start.bat        → opens the menu app
2. Click         🔑 Set API key   → paste hop_... key (app.hoplite.sh → Settings → API Keys)
3. Click         ▶ Start gateway
4. Click         🧪 Test          → wait 30–90 s → "GATEWAY OK"
5. (optional)    🔗 Start ngrok   → public URL, copied to clipboard
```

Manual / Linux / macOS:

```bash
pip install -r requirements.txt
cp config.example.json config.json    # put your api_key inside
python server.py                      # http://127.0.0.1:8787
```

## Connect your AI client

| Setting | Value |
|---------|-------|
| Base URL | `http://127.0.0.1:8787/v1` (or ngrok URL + `/v1`) |
| API key | anything, e.g. `sk-local` |
| Model | `hoplite-opus-5` |

### OMP CLI (`~/.omp/agent/models.yml` + `providers.yml` + `config.yml`)

```yaml
# models.yml → providers:
  hermes-hoplite:
    api: openai-completions
    apiKey: sk-local
    authHeader: true
    baseUrl: http://127.0.0.1:8787/v1
    compat:
      maxTokensField: max_tokens
      supportsStreaming: true
      supportsToolChoice: true
    models:
      - id: hoplite-opus-5
        name: hoplite-opus-5
        contextWindow: 200000
        maxTokens: 16384
        input: [text]
        reasoning: true
        cost: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0}

# providers.yml → providers:
  hermes-hoplite:
    apiKey: sk-local
    baseUrl: http://127.0.0.1:8787/v1
    discoverModels: false
    models:
      - {id: hoplite-opus-5, name: hoplite-opus-5}
    provider: openai

# config.yml
enabledModels:
  - hermes-hoplite/*
```
Restart OMP → `omp /model hermes-hoplite/hoplite-opus-5`
(The menu's 📋 Copy OMP config button gives you this snippet.)

### OpenCode (`~/.config/opencode/opencode.json`)

```json
{
  "provider": {
    "hoplite": {
      "npm": "@ai-sdk/openai-compatible",
      "options": {"baseURL": "http://127.0.0.1:8787/v1", "apiKey": "sk-local"},
      "models": {"hoplite-opus-5": {"name": "Hoplite Opus 5.5"}}
    }
  }
}
```

### curl

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"hoplite-opus-5","messages":[{"role":"user","content":"hi"}],"stream":true}'
```

---

## Features (v3, hardened)

- **Real streaming** — deltas are polled from the agent while it works, not faked after.
- **Conversation continuity** — one Hoplite thread per conversation; follow-up
  messages are appended to the same thread (the agent remembers context).
  Map conversations via the OpenAI `user` field (or first-message hash fallback).
- **Tool calling** — tools are injected into the prompt as a JSON protocol;
  agent tool-call replies are parsed back into OpenAI `tool_calls` format.
  Works with OMP/OpenCode agentic flows (mind the latency below).
- **429/5xx resilience** — exponential backoff + retry; polling tolerates
  transient rate limits until a 240 s deadline.
- **Model mapping** — `hoplite-opus-5` requests `claude-opus-5` on Hoplite;
  invalid model ids auto-fall back to the project default.
- **Web dashboard** at `/` — status, key input, ngrok control, test chat.
- **Admin API** — `POST /admin/config` (hot key change), `POST /admin/ngrok`
  (`{"action":"start|stop|status"}` — tunnel managed by the gateway itself).
- **Concurrency guard** — max 3 parallel agent threads.
- **Project cache** — 5 min TTL, optional `project_id` override.

## Models (probed against the live Hoplite API)

| Gateway model | Hoplite `model` | Notes |
|---------------|-----------------|-------|
| **`hoplite-opus-5`** | `claude-opus-5` | **Claude Opus 5.5** |
| `hoplite-sonnet-5` | `claude-sonnet-5` | |
| `hoplite-opus-4.8` | `claude-opus-4-8` | |
| `hoplite-gpt-5.5` | `gpt-5.5` | |
| `hoplite-gpt-5.6-terra` | `gpt-5.6-terra` | |
| `hoplite-agent` | project default | |
| `<name>-fast` | + `speed: "fast"` | e.g. `hoplite-opus-5-fast` |

`reasoning_effort` (`off…xhigh|max`) maps to Hoplite `reasoning.mode`.
Full walkthrough (Russian): **[GUIDE.md](GUIDE.md)**

**Обратное направление** — дать облачному агенту Hoplite shell и файлы на твоём ПК
(настоящие MCP-инструменты через ngrok-туннель): [`nikita4a/hoplite-pc-bridge`](https://github.com/nikita4a/hoplite-pc-bridge).

## PC Agent — cloud Opus 5.5 with hands on YOUR machine

`pc_agent.py` gives the cloud agent **local tools** (read_file, write_file,
list_dir, run_cmd) executed on your PC. The agent sends tool_calls, this script
runs them locally and returns results — full OpenAI function-calling loop.

```bash
python pc_agent.py "create folder x, put a file in it, show me the result"
python pc_agent.py     # interactive REPL
```

Every tool execution is logged to `pc_agent_log.jsonl`. `run_cmd` runs arbitrary
shell — authorized-lab tool, keep it that way.

The same mechanism powers OMP: when `hoplite-opus-5` is the session model, OMP's
own tools (read/bash/edit) execute locally while the brain runs in the cloud.

## MCP server (built-in, at `/mcp`)

The gateway is ALSO an MCP server — use the cloud agent as a **tool** instead of
a model (no per-message latency for everything, only when you call it):

| Tool | What |
|------|------|
| `hoplite_ask` | blocking: give a task → wait → get the agent's answer (conversation continuity via `conversation` id) |
| `hoplite_task_status` | non-blocking: check a running thread |
| `hoplite_models` | list models/backends |
| `hoplite_conversations` | conversation → thread map |

OMP client config (`~/.omp/agent/mcp.json`):
```json
"hoplite-gw": {
  "type": "http",
  "url": "http://127.0.0.1:8787/mcp",
  "headers": {"Authorization": "Bearer sk-local",
              "Accept": "application/json, text/event-stream"}
}
```

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | dashboard (config, ngrok, test chat) |
| GET | `/health` | status + stats |
| GET | `/setup` | machine-readable instructions |
| GET | `/v1/models` | OpenAI model list |
| POST | `/v1/chat/completions` | OpenAI chat (stream + tools) |
| POST | `/admin/config` | hot-update api_key / project_id |
| POST | `/admin/ngrok` | start/stop/status tunnel |
| POST | `/mcp` | MCP server (streamable HTTP JSON-RPC) |

## Files

| File | Purpose |
|------|---------|
| `server.py` | the whole gateway (single file) |
| `menu.pyw` | Tkinter desktop menu (start/stop/key/ngrok/test/copy configs) |
| `start.bat` | Windows one-click → menu |
| `config.json` | **your secrets — gitignored** |
| `config.example.json` | template |
| `requirements.txt` | fastapi, uvicorn, httpx |

## ⚠️ Latency reality check

Hoplite threads are **cloud coding-agent runs**, not chat completions:
each turn spins a sandbox → expect **30–120 s per message**. Great for
"do this task and answer" usage; not a snappy chat model. Hoplite also
rate-limits thread creation per account (429) — the gateway backs off and
retries automatically.

## Session cookies (needed for conversation continuity)

The `hop_` API key can create/read threads but **cannot append messages**
(Hoplite returns 401 — proven). Appending uses your **browser session cookies +
Origin header** (proven 201). Put them in `config.json`:

```json
{"cookies": {"__Secure-better-auth.session_token": "...", "_iidt": "..."}}
```

Session lives ~7 days (app.hoplite.sh → DevTools → Application → Cookies).
When it expires the gateway logs it and falls back to recreating threads with
serialized history (works, but cold sandbox each round). Refresh cookies weekly.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `auth failed (401)` | new key: app.hoplite.sh → Settings → API Keys |
| `429 after N attempts` | account thread rate limit — wait a few minutes |
| `no Hoplite projects` | create a project at app.hoplite.sh first |
| `agent did not finish within 240s` | check the thread at app.hoplite.sh; raise `DEADLINE` in server.py |
| thread ends `failed` | project needs GitHub App access for repo work; pure Q&A works without |
| ngrok won't start | `winget install ngrok.ngrok`, or put `ngrok_token` in config.json |

## Security

- `config.json` (API key) and any `*_token.json` are **gitignored** — never commit them.
- The gateway binds `127.0.0.1` only; ngrok exposure is explicit and off by default.
- If you ever commit a key by accident: rotate it immediately in the Hoplite dashboard.
