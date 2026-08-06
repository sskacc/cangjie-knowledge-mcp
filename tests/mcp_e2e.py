"""End-to-end MCP stdio client test: initialize -> tools/list -> tools/call."""

import json
import subprocess
import sys
import os

PY = sys.executable
server_script = os.path.join(os.path.dirname(__file__), "..", "src", "cjkb", "mcp_server.py")

env = dict(os.environ)
env["PYTHONPATH"] = os.path.join(os.path.dirname(__file__), "..", "src")
env["CJKB_DATA_DIR"] = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "data"))

proc = subprocess.Popen(
    [PY, server_script, "--data-dir", env["CJKB_DATA_DIR"]],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, env=env,
)


def rpc(msg):
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    return json.loads(line) if line.strip() else None


# 1. initialize
resp = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test"}}})
print("initialize:", resp["result"]["serverInfo"])

# 2. tools/list
resp = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
tools = [t["name"] for t in resp["result"]["tools"]]
print("tools:", tools)
assert len(tools) == 7, tools

# 3. tools/call search_api
resp = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "search_api", "arguments": {"query": "HashMap put key value", "top_k": 3}}})
print("search_api isError:", resp["result"]["isError"])
data = json.loads(resp["result"]["content"][0]["text"])
print("  hits:", [(r["name"], r["module"]) for r in data["results"][:3]])

# 4. java_to_cangjie
resp = rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "java_to_cangjie", "arguments": {"java_symbol": "java.util.List"}}})
data = json.loads(resp["result"]["content"][0]["text"])
print("java_to_cangjie:", [(m["java"], m["cangjie"]) for m in data["results"][:3]])

# 5. error_fix_hint
resp = rpc({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "error_fix_hint", "arguments": {"error_text": "cannot find symbol println in std.io"}}})
data = json.loads(resp["result"]["content"][0]["text"])
print("error_fix_hint apis:", [(r["name"], r["module"]) for r in data["apis"][:3]])

# 6. find_examples
resp = rpc({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
            "params": {"name": "find_examples", "arguments": {"query": "read file"}}})
data = json.loads(resp["result"]["content"][0]["text"])
print("find_examples:", [e["title"] for e in data["results"][:3]])

# 7. unknown method
resp = rpc({"jsonrpc": "2.0", "id": 7, "method": "bogus/method", "params": {}})
print("unknown method error code:", resp["error"]["code"])

proc.stdin.close()
proc.wait(timeout=10)
print("server exit code:", proc.returncode)
print("ALL MCP TESTS PASSED")
