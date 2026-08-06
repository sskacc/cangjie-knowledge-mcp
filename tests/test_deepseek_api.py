"""Direct test of the DeepSeek API endpoint used by example_writer."""
import json
import os
import urllib.request
import urllib.error

API_KEY = os.environ["OPENAI_API_KEY"]
BASE = os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com")
MODEL = os.environ.get("OPENAI_MODEL", "deepseek-v4-flash")

url = BASE.rstrip("/") + "/chat/completions"
payload = {
    "model": MODEL,
    "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
    "max_tokens": 20,
}
req = urllib.request.Request(
    url, data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=60) as resp:
        print("STATUS:", resp.status)
        body = json.loads(resp.read().decode())
        print("choices[0].message:", body["choices"][0]["message"]["content"][:200])
        print("usage:", body.get("usage"))
except urllib.error.HTTPError as e:
    print("HTTP ERROR:", e.code)
    print(e.read().decode()[:1000])
except Exception as e:
    print("ERR:", repr(e))
