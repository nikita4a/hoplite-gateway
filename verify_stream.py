"""Live SSE check: the gateway must stream the answer EXACTLY ONCE.

Regression guard for the duplicated-final-chunk bug (dead `buffer` accumulator
in _stream_run). Offline unit test lives in tests_gateway.py; this one proves
it over the wire, with the stream_options opencode always sends.

Usage: python verify_stream.py [MARK]
"""
import json
import sys
import time
import urllib.request

GATEWAY = "http://127.0.0.1:8787/v1/chat/completions"
MARK = sys.argv[1] if len(sys.argv) > 1 else "STREAMDUP-4417"


def stream_once(mark: str, timeout: int = 280) -> tuple[str, int, list, bool]:
    payload = {
        "model": "hoplite-opus-5",
        "messages": [{"role": "user",
                      "content": f"Reply with exactly this token and nothing else: {mark}"}],
        "stream": True,
        "stream_options": {"include_usage": True},   # opencode forces this
        "user": "verify-stream-dup",
    }
    req = urllib.request.Request(
        GATEWAY, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer sk-local",
                 "Accept": "text/event-stream"})
    pieces, finishes, heartbeats, done = [], [], 0, False
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
            if choice.get("finish_reason"):
                finishes.append(choice["finish_reason"])
    return "".join(pieces), heartbeats, finishes, done


def main() -> int:
    t0 = time.time()
    full, heartbeats, finishes, done = stream_once(MARK)
    hits = full.count(MARK)
    print(f"elapsed {time.time() - t0:.1f}s  chars={len(full)}  "
          f"heartbeats={heartbeats}  finish_reason={finishes}  DONE={done}")
    print(f"assembled ({len(full)} chars): {full!r}")
    print(f"mark occurrences: {hits}")
    ok = hits == 1 and done and finishes == ["stop"]
    print("VERDICT:", "OK - streamed exactly once" if ok else "FAIL - duplicated/malformed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
