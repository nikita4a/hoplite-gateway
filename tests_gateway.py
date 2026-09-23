"""Offline unit tests for the gateway (no network, no Hoplite calls).
Run: python tests_gateway.py
"""
import asyncio
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

spec = importlib.util.spec_from_file_location("srv", "server.py")
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)


def test_parse_tool_calls():
    t = srv._parse_tool_calls('{"tool_calls": [{"name": "get_weather", "arguments": {"city": "Moscow"}}]}')
    assert t and t[0]["function"]["name"] == "get_weather"
    assert json.loads(t[0]["function"]["arguments"]) == {"city": "Moscow"}

    t = srv._parse_tool_calls('```json\n{"tool_calls": [{"name": "read_file", "arguments": {"path": "a.py"}}]}\n```')
    assert t and t[0]["function"]["name"] == "read_file"

    t = srv._parse_tool_calls('I will call a tool:\n{"tool_calls": [{"name": "x", "arguments": {}}]}\nDone.')
    assert t and t[0]["function"]["name"] == "x"

    t = srv._parse_tool_calls('{"tool_calls": [{"name": "a", "arguments": {}}, {"name": "b", "arguments": {"k": 1}}]}')
    assert t and len(t) == 2 and t[1]["function"]["name"] == "b"

    assert srv._parse_tool_calls("Just a normal answer.") is None
    assert srv._parse_tool_calls('{"tool_calls": [{"name": broken') is None

    t = srv._parse_tool_calls('{"tool_calls": [{"function": {"name": "y", "arguments": {"z": 2}}}]}')
    assert t and t[0]["function"]["name"] == "y"


def test_wrap_tools():
    tools = [{"type": "function", "function": {
        "name": "calc", "description": "Calculator",
        "parameters": {"type": "object", "properties": {"expr": {"type": "string"}}, "required": ["expr"]}}}]
    p = srv._wrap_tools("What is 2+2?", tools)
    assert "calc" in p and "tool_calls" in p and p.startswith("What is 2+2?")
    assert srv._wrap_tools("hi", []) == "hi"


def test_conv_key():
    m1 = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"},
          {"role": "user", "content": "bye"}]
    m2 = [{"role": "user", "content": "hello"}, {"role": "user", "content": "another"}]
    assert srv._conv_key(m1, None) == srv._conv_key(m2, None)
    assert srv._conv_key(m1, "u1") == "user:u1"


def test_render_history():
    h = srv._render_history([{"role": "user", "content": "x" * 5000}])
    assert len(h) < 1600


def test_latest_assistant():
    msgs = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "q2"},
        {"role": "tool", "content": "setup noise"},
        {"role": "assistant", "content": "new answer"},
    ]
    assert srv._latest_assistant(msgs) == "new answer"


def test_ask_timeout_envelope():
    """Sentinel's check: wait_s floor allows 1.0 + partial envelope, mocked (no network)."""
    import asyncio

    seen = {}

    async def fake_ask(prompt, conv, model="hoplite-agent", reasoning=None,
                       on_delta=None, wait_s=None):
        seen["wait_s"] = wait_s
        return "thr_fake123", None      # None = still running → envelope path

    orig = srv._ask_agent
    srv._ask_agent = fake_ask
    try:
        out = asyncio.run(srv._mcp_dispatch(
            "hoplite_ask", {"prompt": "x", "conversation": "t", "wait_s": 1}))
    finally:
        srv._ask_agent = orig

    assert seen["wait_s"] == 1.0, f"floor must allow 1.0, got {seen['wait_s']}"
    assert "[still running after 1s]" in out, out
    assert "thr_fake123" in out and "hoplite_task_status" in out, out


def test_ask_returns_answer_directly():
    import asyncio

    async def fake_ask(prompt, conv, model="hoplite-agent", reasoning=None,
                       on_delta=None, wait_s=None):
        return "thr_x", "FINAL-ANSWER"

    orig = srv._ask_agent
    srv._ask_agent = fake_ask
    try:
        out = asyncio.run(srv._mcp_dispatch("hoplite_ask", {"prompt": "x"}))
    finally:
        srv._ask_agent = orig
    assert out == "FINAL-ANSWER"


def test_slim_schema_and_wrap_cap():
    """Big toolsets (OMP sends dozens of tools) must fit: descriptions trimmed, cap 20000."""
    big = [{"type": "function", "function": {
        "name": f"tool_{i}",
        "description": "D" * 5000,
        "parameters": {"type": "object",
                       "properties": {"a": {"type": "string", "description": "x" * 900}},
                       "required": ["a"]}}} for i in range(10)]
    wrapped = srv._wrap_tools("task", big)
    assert wrapped.startswith("task")
    assert len(wrapped) <= 20000 + 800          # cap + instruction text
    for i in range(10):                          # all tool names survived
        assert f"tool_{i}" in wrapped
    slim = srv._slim_schema({"description": "y" * 500, "type": "object"})
    assert len(slim["description"]) <= 120


def _sse_contents(agen):
    """Collect all delta.content strings from _stream_run SSE chunks."""
    out = []

    async def go():
        async for chunk in agen:
            for line in chunk.splitlines():
                if not line.startswith("data: ") or line.startswith("data: [DONE]"):
                    continue
                payload = json.loads(line[6:])
                for ch in payload["choices"]:
                    d = ch.get("delta", {})
                    if isinstance(d.get("content"), str):
                        out.append(d["content"])

    asyncio.run(go())
    return out


def test_stream_deltas_not_duplicated():
    """Every delta streamed once; the final flush must not re-emit the whole text."""
    async def fake_run(client, tid, on_delta=None):
        await on_delta("Hello ")
        await on_delta("world")
        return "Hello world"

    orig = srv._run_agent
    srv._run_agent = fake_run
    try:
        content = "".join(_sse_contents(srv._stream_run(None, "cid", "m", "tid", None)))
    finally:
        srv._run_agent = orig
    assert content == "Hello world", f"expected 'Hello world' exactly once, got {content!r}"


def test_stream_flush_when_no_deltas():
    """Agent output with zero deltas must still be emitted exactly once."""
    async def fake_run(client, tid, on_delta=None):
        return "Final only"

    orig = srv._run_agent
    srv._run_agent = fake_run
    try:
        content = "".join(_sse_contents(srv._stream_run(None, "cid", "m", "tid", None)))
    finally:
        srv._run_agent = orig
    assert content == "Final only", f"expected 'Final only' exactly once, got {content!r}"


def test_save_config_preserves_unknown_keys():
    """Saving one key must not delete the other eight pre-existing keys."""
    nine = {
        "api_key": "hop_old",
        "api_base": "https://api.hoplite.sh",
        "project_id": "proj_x",
        "ngrok_token": "",
        "cookies": {"hop_session": "abc123", "nested": {"k": "v"}},
        "gateway_key": "gk_1",
        "deadline_s": 540,
        "queue_limit": 4,
        "mcp_ask_wait_s": 120,
    }
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "config.json"
        tmp.write_text(json.dumps(nine), encoding="utf-8")
        orig = srv.CONFIG_FILE
        srv.CONFIG_FILE = tmp
        try:
            srv._save_config({"api_key": "hop_test1234abcd"})
            got = srv._load_config()
        finally:
            srv.CONFIG_FILE = orig
        for k, v in nine.items():
            if k == "api_key":
                assert got[k] == "hop_test1234abcd", f"api_key not updated: {got.get(k)!r}"
            else:
                assert k in got, f"key {k} deleted by save"
                assert got[k] == v, f"{k} changed: {v!r} -> {got[k]!r}"


def test_missing_api_key_returns_401():
    """No API key → 401 auth_error envelope, not a 500 NameError."""
    orig = srv.API_KEY
    srv.API_KEY = ""
    try:
        resp = asyncio.run(srv._complete(
            {"model": "hoplite-opus-5",
             "messages": [{"role": "user", "content": "hi"}]}))
    finally:
        srv.API_KEY = orig
    assert resp.status_code == 401, f"expected 401, got {resp.status_code}"
    body = json.loads(resp.body)
    assert body["error"]["type"] == "auth_error", body
    assert "No API key" in body["error"]["message"]


def test_ngrok_uses_env_authtoken():
    """NGROK_AUTHTOKEN is the last fallback after body.token and CONFIG."""
    captured = {}

    def fake_popen(args, **kw):
        captured["args"] = list(args)
        raise FileNotFoundError("ngrok not installed")   # abort before the 15s poll loop

    class Req:
        async def json(self):
            return {"action": "start", "token": ""}

    orig_popen = srv.subprocess.Popen
    orig_token = srv.CONFIG.get("ngrok_token")
    orig_env = os.environ.get("NGROK_AUTHTOKEN")
    srv.subprocess.Popen = fake_popen
    srv.CONFIG["ngrok_token"] = ""
    os.environ["NGROK_AUTHTOKEN"] = "tok-env-123"
    srv._ngrok_proc = None
    srv._ngrok_url = ""
    try:
        asyncio.run(srv.admin_ngrok(Req()))
    finally:
        srv.subprocess.Popen = orig_popen
        srv.CONFIG["ngrok_token"] = orig_token
        if orig_env is None:
            os.environ.pop("NGROK_AUTHTOKEN", None)
        else:
            os.environ["NGROK_AUTHTOKEN"] = orig_env
    assert captured.get("args"), "Popen was never called"
    i = captured["args"].index("--authtoken")
    assert captured["args"][i + 1] == "tok-env-123", captured["args"]


def test_stream_response_owns_its_client():
    """FastAPI consumes a StreamingResponse generator AFTER the handler returns, so the
    httpx client must be opened INSIDE the generator.

    Regression: _complete returned StreamingResponse(_stream_run(c, ...)) from inside
    `async with _client() as c`. The client was closed on return, every poll inside
    _run_agent raised, `except Exception: pass` swallowed it, and the gateway spun to
    DEADLINE (540 s) although the agent had answered in ~50 s. Non-streaming worked;
    streaming never did — and streaming is what opencode/OMP always send.
    """
    import inspect
    assert inspect.isasyncgenfunction(srv._stream_owned), "_stream_owned must be an async generator"
    src = inspect.getsource(srv._complete)
    assert "_stream_owned(" in src, "the stream branch must hand off to _stream_owned"
    assert "_stream_run(c," not in src, "the stream branch must not leak the handler-scoped client"

    events = []
    observed = {}
    sem_before = srv._sem._value      # Semaphore.locked() is False while any slot is free

    class FakeClient:
        async def __aenter__(self):
            events.append("open")
            return self

        async def __aexit__(self, *exc):
            events.append("close")
            return False

    async def fake_stream_run(c, cid, model, tid, tools):
        events.append("streaming")
        observed["sem_value"] = srv._sem._value
        yield "data: one\n\n"
        yield "data: two\n\n"

    orig_client, orig_run = srv._client, srv._stream_run
    srv._client = lambda: FakeClient()
    srv._stream_run = fake_stream_run
    try:
        async def go():
            return [chunk async for chunk in srv._stream_owned("cid", "hoplite-opus-5", "thr_x", None)]
        chunks = asyncio.run(go())
    finally:
        srv._client, srv._stream_run = orig_client, orig_run

    assert chunks == ["data: one\n\n", "data: two\n\n"], chunks
    # client open BEFORE the first chunk, closed only AFTER the last one
    assert events == ["open", "streaming", "close"], events
    assert observed["sem_value"] == sem_before - 1, \
        "the concurrency guard must be held for the whole stream"


def test_busy_gateway_returns_429_fast():
    """No request may wait forever when all concurrency slots are held."""
    import time
    orig_to = srv.SEM_ACQUIRE_TIMEOUT
    srv.SEM_ACQUIRE_TIMEOUT = 1          # keep the test fast
    t0 = time.time()

    async def scenario():
        for _ in range(srv.MAX_CONCURRENT):
            await srv._sem.acquire()     # drain every slot
        try:
            return await srv._complete({
                "model": "hoplite-opus-5",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            })
        finally:
            for _ in range(srv.MAX_CONCURRENT):
                srv._sem.release()

    try:
        resp = asyncio.run(scenario())
    finally:
        srv.SEM_ACQUIRE_TIMEOUT = orig_to
    assert resp.status_code == 429, f"expected 429, got {resp}"
    assert time.time() - t0 < 10, "must fail fast instead of waiting for a slot"


def _sse_chunks(agen):
    """Parse every SSE data frame into its choice dict (delta + finish_reason)."""
    out = []

    async def go():
        async for chunk in agen:
            for line in chunk.splitlines():
                if not line.startswith("data: ") or line.startswith("data: [DONE]"):
                    continue
                payload = json.loads(line[6:])
                out.extend(payload["choices"])

    asyncio.run(go())
    return out


def test_stream_with_tools_does_not_leak_protocol_json():
    """With tools in play the raw protocol JSON must never reach the client as text.

    Real failure (omp, live): the tool_calls JSON was streamed as content deltas AND
    executed as a tool call, so the transcript showed '{"tool_calls": [...]}' above the
    rendered Read tree. Non-streaming cleared the text (`if calls: text = ""`); the
    stream path cannot retract deltas it already sent, so it must withhold them.
    """
    payload = '{"tool_calls": [{"name": "read", "arguments": {"path": "a.py"}}]}'

    async def fake_run(client, tid, on_delta=None):
        await on_delta(payload[:20])
        await on_delta(payload[20:])
        return payload

    tools = [{"type": "function", "function": {"name": "read"}}]
    orig = srv._run_agent
    srv._run_agent = fake_run
    try:
        chunks = _sse_chunks(srv._stream_run(None, "cid", "m", "tid", tools))
    finally:
        srv._run_agent = orig

    content = "".join(c.get("delta", {}).get("content") or "" for c in chunks)
    assert content == "", f"protocol JSON leaked into stream content: {content!r}"
    tc = [c for c in chunks if c.get("delta", {}).get("tool_calls")]
    assert len(tc) == 1, f"expected exactly one tool_calls chunk, got {len(tc)}"
    assert tc[0]["delta"]["tool_calls"][0]["function"]["name"] == "read"
    assert any(c.get("finish_reason") == "tool_calls" for c in chunks)


def test_stream_with_tools_prose_delivered_once():
    """Tools present but the agent answered plain prose: text arrives exactly once."""
    async def fake_run(client, tid, on_delta=None):
        await on_delta("Just ")
        await on_delta("prose")
        return "Just prose"

    tools = [{"type": "function", "function": {"name": "read"}}]
    orig = srv._run_agent
    srv._run_agent = fake_run
    try:
        chunks = _sse_chunks(srv._stream_run(None, "cid", "m", "tid", tools))
    finally:
        srv._run_agent = orig

    content = "".join(c.get("delta", {}).get("content") or "" for c in chunks)
    assert content == "Just prose", content
    assert any(c.get("finish_reason") == "stop" for c in chunks)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} test groups passed")
