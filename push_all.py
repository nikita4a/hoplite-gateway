"""Push all gateway files to a GitHub repo via API (git-remote-https broken locally).

Usage:  python push_all.py <owner> <repo> [branch]
Token:  GITHUB_PAT env var (required).

Creates the repo if missing, then replaces the tree with the local file set
(secrets excluded by an explicit allowlist).
"""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

TOKEN = os.getenv("GITHUB_PAT", "")
if not TOKEN:
    sys.exit("[!] set GITHUB_PAT env var")

H = {"Authorization": f"token {TOKEN}", "User-Agent": "hoplite-gw",
     "Accept": "application/vnd.github+json"}

# only these files ever leave the machine (secrets stay home)
ALLOW = ["server.py", "menu.pyw", "start.bat", "README.md", "requirements.txt",
         "config.example.json", "opencode_config.json", ".gitignore", "push_all.py", "tests_gateway.py"]


def gh(method: str, path: str, data=None, raw_body=None, retries: int = 4):
    for a in range(retries):
        try:
            body = raw_body if raw_body is not None else (
                json.dumps(data).encode() if data is not None else None)
            req = urllib.request.Request(f"https://api.github.com{path}", data=body,
                                         method=method, headers=H)
            with urllib.request.urlopen(req, timeout=60) as r:
                out = r.read()
                return json.loads(out) if out else {}
        except urllib.error.HTTPError as e:
            msg = e.read().decode()[:200]
            if e.code in (403, 429) and a < retries - 1:
                time.sleep(5 * (a + 1))
                continue
            print(f"  HTTP {e.code} {method} {path}: {msg}")
            return None
        except Exception as e:
            if a < retries - 1:
                time.sleep(3)
                continue
            print(f"  FAIL {method} {path}: {type(e).__name__} {e}")
            return None


def main():
    owner, repo = sys.argv[1], sys.argv[2]
    branch = sys.argv[3] if len(sys.argv) > 3 else "main"
    base = os.path.dirname(os.path.abspath(__file__))

    # 1. repo exists?
    info = gh("GET", f"/repos/{owner}/{repo}")
    if info is None:
        print(f"[*] creating {owner}/{repo} …")
        info = gh("POST", "/user/repos", {"name": repo, "private": False,
                                          "description": "Hoplite cloud agents (Opus 5.5) as an OpenAI v1 API — gateway + desktop menu + ngrok",
                                          "auto_init": True})
        if not info:
            sys.exit("repo create failed")
        time.sleep(2)
    print(f"[*] repo: {info.get('html_url')}")

    # 2. current head (repo may be freshly initialized)
    ref = gh("GET", f"/repos/{owner}/{repo}/git/ref/heads/{branch}")
    parent, base_tree = None, None
    if ref:
        parent = ref["object"]["sha"]
        commit = gh("GET", f"/repos/{owner}/{repo}/git/commits/{parent}")
        base_tree = commit["tree"]["sha"] if commit else None

    # 3. blobs
    tree_entries = []
    for name in ALLOW:
        p = os.path.join(base, name)
        if not os.path.exists(p):
            print(f"  skip (missing): {name}")
            continue
        with open(p, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        blob = gh("POST", f"/repos/{owner}/{repo}/git/blobs",
                  raw_body=json.dumps({"content": b64, "encoding": "base64"}).encode())
        if not blob:
            sys.exit(f"blob failed: {name}")
        tree_entries.append({"path": name, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        print(f"  + {name} ({len(b64)//1024}KB b64)")

    # 4. remove known secret files if present in remote tree
    for secret in ("mcp_token.json", "cookies.json", "config.json"):
        tree_entries.append({"path": secret, "mode": "100644", "type": "blob",
                             "sha": "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"})  # empty blob = purge content

    # 5. tree + commit + ref
    payload = {"tree": tree_entries}
    if base_tree:
        payload["base_tree"] = base_tree
    tree = gh("POST", f"/repos/{owner}/{repo}/git/trees", payload)
    if not tree:
        sys.exit("tree failed")
    commit_body = {"message": "v3: hardened gateway — real streaming, thread reuse, tool protocol, "
                              "429 backoff, dashboard, Tkinter menu, ngrok control; secrets purged",
                   "tree": tree["sha"]}
    if parent:
        commit_body["parents"] = [parent]
    commit = gh("POST", f"/repos/{owner}/{repo}/git/commits", commit_body)
    if not commit:
        sys.exit("commit failed")
    if ref:
        upd = gh("PATCH", f"/repos/{owner}/{repo}/git/refs/heads/{branch}",
                 {"sha": commit["sha"], "force": True})
    else:
        upd = gh("POST", f"/repos/{owner}/{repo}/git/refs",
                 {"ref": f"refs/heads/{branch}", "sha": commit["sha"]})
    if upd is None:
        sys.exit("ref update failed")
    print(f"\nPUSHED → https://github.com/{owner}/{repo}/commit/{commit['sha']}")


if __name__ == "__main__":
    main()
