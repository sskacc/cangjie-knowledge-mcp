"""E2E layered search WITH real LLM (deepseek-v4-flash)."""
import json, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from cjkb.mcp_server import McpServer
from cjkb.config import load_config
from cjkb.index.searcher import Searcher

cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config.yaml"))
print("LLM config:", {k: ("***" if k == "api_key" else v) for k, v in cfg.get("llm", {}).items()})
data_dir = os.environ.get("DATA_DIR", cfg["output"]["data_dir"])
s = Searcher.load(data_dir, cfg)
server = McpServer(s, cfg)

def call(name, args):
    resp = server.handle_message({"jsonrpc": "2.0", "id": 99, "method": "tools/call",
                                  "params": {"name": name, "arguments": args}})
    assert not resp["result"]["isError"], resp["result"]
    return json.loads(resp["result"]["content"][0]["text"])

tests = [
    ("readLine on a reader", "String line = reader.readLine();"),
    ("HashMap put", "map.put(key, value);"),
    ("file copy loop", "while ((len = in.read(buf)) > 0) { out.write(buf, 0, len); }"),
]

for label, code in tests:
    print(f"\n########## {label} ##########")
    r = call("describe_java_code", {"java_code": code})
    print("  NL api      :", r["nl"]["api"])
    print("  NL statement:", r["nl"]["statement"])
    print("  NL function :", r["nl"]["function"])
    r = call("resolve_java_code", {"java_code": code, "top_k": 3})
    print("  best_level:", r["best_level"])
    for lvl in ("api", "statement", "function"):
        l = r["levels"][lvl]
        print(f"    [{lvl}] score={l['score']} query='{l['query'][:70]}'")
        for a in l["apis"][:2]:
            print(f"         api: {a['name']} | {a['module']} | {a['signature'][:55]}")

print("\nDONE")
