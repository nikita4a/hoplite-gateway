"""Refresh the Hoplite MCP OAuth token and update OMP's mcp.json.
Run whenever OMP's hoplite MCP tools start failing with 401 (token lives ~1h).

Usage:  python refresh_hoplite_mcp.py
Needs:  Hoplite CLI installed (hoplite mcp start does the OAuth silently when
        the browser session is already logged in to app.hoplite.sh).
"""
import json
import subprocess
import sys
import time
from pathlib import Path

TOKEN_FILE = Path.home() / ".config" / "hoplite" / "mcp-oauth.json"
MCP_JSON = Path.home() / ".omp" / "agent" / "mcp.json"
HOPLITE_EXE = Path.home() / "AppData/Local/Hoplite/bin/hoplite.exe"


def main():
    exe = str(HOPLITE_EXE) if HOPLITE_EXE.exists() else "hoplite"
    print("[*] running: hoplite mcp start (OAuth refresh)…")
    r = subprocess.run([exe, "mcp", "start"], capture_output=True, text=True, timeout=120)
    if not TOKEN_FILE.exists():
        print("[!] token file not found:", TOKEN_FILE)
        print(r.stdout, r.stderr)
        sys.exit(1)

    tok = json.loads(TOKEN_FILE.read_text())
    access = tok["accessToken"]
    print(f"[*] token refreshed, expires {tok.get('expiresAt')}")

    mcp = json.loads(MCP_JSON.read_text(encoding="utf-8"))
    entry = {
        "type": "http",
        "url": "https://api.hoplite.sh/mcp",
        "headers": {"Authorization": f"Bearer {access}",
                    "Accept": "application/json, text/event-stream"},
    }
    if "mcpServers" in mcp:
        mcp["mcpServers"]["hoplite"] = entry
    else:
        mcp["hoplite"] = entry
    MCP_JSON.write_text(json.dumps(mcp, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[*] {MCP_JSON} updated — restart OMP to pick up the new token")


if __name__ == "__main__":
    main()
