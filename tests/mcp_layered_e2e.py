"""E2E test for resolve_java_code (per-call suggestions) / describe_java_code."""
import json
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from cjkb.mcp_server import McpServer
from cjkb.config import load_config
from cjkb.index.searcher import Searcher

cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config.yaml"))
data_dir = os.environ.get("DATA_DIR", cfg["output"]["data_dir"])
s = Searcher.load(data_dir, cfg)
server = McpServer(s, cfg)

def call(name, args):
    resp = server.handle_message({"jsonrpc": "2.0", "id": 99, "method": "tools/call",
                                  "params": {"name": name, "arguments": args}})
    assert not resp["result"]["isError"], resp
    return json.loads(resp["result"]["content"][0]["text"])

def print_suggestions(r):
    for i, sg in enumerate(r["suggestions"]):
        lvl = sg.get("level", "?")
        expr = sg.get("java_expr", "?")
        if lvl == "api":
            members = [m["name"] for m in sg.get("members", [])]
            print(f"  sug[{i}] api        expr={expr!r}")
            print(f"         type={sg.get('cangjie_type')} @ {sg.get('module')} conf={sg.get('confidence')}")
            print(f"         members={members}")
        elif lvl == "statement":
            apis = [a["name"] for a in sg.get("apis", [])]
            print(f"  sug[{i}] statement  expr={expr!r} apis={apis}")
        else:
            sug = sg.get("suggested")
            if sug:
                print(f"  sug[{i}] function   expr={expr!r} block-suggest={sug.get('cangjie_type')} @ {sug.get('module')}")
            else:
                print(f"  sug[{i}] function   expr={expr!r} suggested=None")

# 1. describe_java_code: NL descriptions at 3 granularities
print("=== describe_java_code ===")
r = call("describe_java_code", {"java_code": "String line = reader.readLine();"})
print("detected_level:", r["detected_level"])
print("api NL      :", r["nl"]["api"])
print("statement NL:", r["nl"]["statement"])
print("function NL :", r["nl"]["function"])

# 2. resolve_java_code: single call, expected api-level suggest
print("\n=== resolve_java_code: BufferedReader.readLine ===")
r = call("resolve_java_code", {"java_code": "String line = reader.readLine();", "top_k": 3})
print("calls:", [(c.get("receiver"), c.get("method")) for c in r["calls"]])
print_suggestions(r)

# 3. multi-API block: each call gets its own suggest
print("\n=== resolve_java_code: multi-API block ===")
r = call("resolve_java_code", {"java_code": """
BufferedReader reader = new BufferedReader(new InputStreamReader(System.in));
String line = reader.readLine();
HashMap<String, Integer> map = new HashMap<>();
map.put("a", 1);
Integer v = map.get("a");
""", "top_k": 3})
print("calls:", [(c.get("receiver"), c.get("method")) for c in r["calls"]])
print_suggestions(r)

print("\nALL LAYERED-SEARCH E2E TESTS PASSED")
