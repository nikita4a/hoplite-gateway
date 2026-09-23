"""Live SSE checks against the running gateway (127.0.0.1:8787).

    python verify_stream.py              # plain turn: answer streamed exactly once
    python verify_stream.py --tools      # tool turn: protocol JSON must NOT leak as text
    python verify_stream.py [MARK]       # custom marker token

Regression guards for two bugs that were reproduced live:
  1. duplicated final chunk (dead `buffer` accumulator) -> answer sent twice
  2. with tools, the raw {"tool_calls": ...} protocol JSON was streamed as content
     AND executed as a tool call (seen in omp: JSON printed above the Read tree)

Offline unit tests live in tests_gateway.py; this file proves it over the wire,
with the stream_options that opencode/omp always send.
"""
import json
import sys
import time
import urllib.request

GATEWAY = "http://127.0.0.1:8787/v1/chat/completions"
MARK = next((a for a in sys.argv[1:] if not a.startswith("--")), "STREAMDUP-4417")
USE_TOOLS = "--tools" in sys.argv

TOOLS = [{
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read a file from the local disk",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "file path"}},
            "required": ["path"],
        },
    },
}]


def stream_once(mark: str, tools=None, timeout: int = 280):
    """Returns (content, heartbeats, finish_reasons, done, tool_call_chunks)."""
    if tools:
        content = (f"Use the read_file tool on path {mark}.txt. "
                   f"Reply ONLY with the tool-call JSON, no prose.")
    else:
        content = f"Reply with exactly this token and nothing else: {mark}"
    payload = {
        "model": "hoplite-opus-5",
        "messages": [{"role": "user", "content": content}],
        "stream": True,
        "stream_options": {"include_usage": True},   # opencode/omp force this
        "user": "verify-stream-dup",
    }
    if tools:
        payload["tools"] = tools
    req = urllib.request.Request(
        GATEWAY, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer sk-local",
                 "Accept": "text/event-stream"})
    pieces, finishes, calls = [], [], []
    heartbeats, done = 0, False
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if line.startswith(": "):
                heartbeats += 1
                continue
            if not line.startswith("data: "):
                continue
            data = line[6:]
            if data.strip() == "[DONE]":
                done = True
                break
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            choice = (obj.get("choices") or [{}])[0]
            delta = choice.get("delta") or {}
            if delta.get("content"):
                pieces.append(delta["content"])
            if delta.get("tool_calls"):
                calls.extend(delta["tool_calls"])
            if choice.get("finish_reason"):
                finishes.append(choice["finish_reason"])
    return "".join(pieces), heartbeats, finishes, done, calls


def main() -> int:
    t0 = time.time()
    full, heartbeats, finishes, done, calls = stream_once(
        MARK, tools=TOOLS if USE_TOOLS else None)
    elapsed = time.time() - t0
    print(f"mode={'tools' if USE_TOOLS else 'plain'}  elapsed {elapsed:.1f}s  "
          f"chars={len(full)}  heartbeats={heartbeats}  "
          f"finish_reason={finishes}  DONE={done}")

    if USE_TOOLS:
        names = [c.get("function", {}).get("name") for c in calls]
        print(f"tool_call chunks: {len(calls)} names={names}")
        print(f"content: {full[:300]!r}")
        leaked = '"tool_calls"' in full
        if calls:
            ok = done and not leaked
            verdict = ("OK - tool_calls delivered, no protocol JSON in content" if ok
                       else f"FAIL - protocol JSON leaked into content: {full[:200]!r}")
        else:
            # The cloud agent may ignore the protocol and answer in prose instead.
            # That is its choice; the invariant we own is "no raw JSON as text".
            ok = done and not leaked
            verdict = ("OK - agent answered in prose (no tool_calls), no JSON leaked"
                       if ok else f"FAIL - protocol JSON leaked: {full[:200]!r}")
    else:
        hits = full.count(MARK)
        print(f"assembled: {full!r}")
        print(f"mark occurrences: {hits}")
        ok = hits == 1 and done and finishes == ["stop"]
        verdict = ("OK - streamed exactly once" if ok
                   else "FAIL - duplicated/malformed")

    print("VERDICT:", verdict)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
