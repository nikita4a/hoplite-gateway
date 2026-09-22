"""Offline unit tests for the gateway (no network, no Hoplite calls).
Run: python tests_gateway.py
"""
import importlib.util
import json
import sys

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


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} test groups passed")
