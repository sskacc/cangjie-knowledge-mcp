"""E2E test for the new layered-search tools (resolve_java_code / describe_java_code)."""
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

# 1. describe_java_code: NL descriptions at 3 granularities
print("=== describe_java_code ===")
r = call("describe_java_code", {"java_code": "String line = reader.readLine();"})
print("detected_level:", r["detected_level"])
print("api NL      :", r["nl"]["api"])
print("statement NL:", r["nl"]["statement"])
print("function NL :", r["nl"]["function"])

# 2. resolve_java_code: layered retrieval (heuristic, no LLM in this env)
print("\n=== resolve_java_code: BufferedReader.readLine ===")
r = call("resolve_java_code", {"java_code": "String line = reader.readLine();", "top_k": 3})
print("best_level:", r["best_level"])
for lvl in ("api", "statement", "function"):
    l = r["levels"][lvl]
    print(f"  [{lvl}] score={l['score']} query='{l['query'][:60]}'")
    for a in l["apis"][:2]:
        print(f"       api: {a['name']} | {a['module']} | {a['signature'][:55]}")
print("best_hit:", r["best_hit"]["name"], "|", r["best_hit"]["module"])

# 3. whole-function level
print("\n=== resolve_java_code: whole function ===")
r = call("resolve_java_code", {"java_code": """
public void copyFile(File src, File dst) throws IOException {
    InputStream in = new FileInputStream(src);
    OutputStream out = new FileOutputStream(dst);
    byte[] buf = new byte[1024];
    int len;
    while ((len = in.read(buf)) > 0) {
        out.write(buf, 0, len);
    }
    in.close();
    out.close();
}
""", "top_k": 3})
print("best_level:", r["best_level"])
for lvl in ("api", "statement", "function"):
    l = r["levels"][lvl]
    print(f"  [{lvl}] score={l['score']} query='{l['query'][:70]}'")
    for a in l["apis"][:2]:
        print(f"       api: {a['name']} | {a['module']} | {a['signature'][:55]}")

print("\nALL LAYERED-SEARCH E2E TESTS PASSED")
