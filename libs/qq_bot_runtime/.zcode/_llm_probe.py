# ASCII only: test DeepSeek API via proxy and direct
import asyncio, os
import httpx

KEY = None
BASE = "https://api.deepseek.com"
import sys
sys.path.insert(0, r"F:\qq_bot")
import config
KEY = config.DEEPSEEK_API_KEY
BASE = config.DEEPSEEK_BASE_URL.rstrip("/")

BODY = {
    "model": config.DEEPSEEK_MODEL,
    "messages": [{"role": "user", "content": "hi"}],
    "stream": False,
    "max_tokens": 8,
    "thinking": {"type": "disabled"},
}
HDR = {"Authorization": "Bearer " + KEY, "Content-Type": "application/json"}

async def test(name, trust_env):
    try:
        async with httpx.AsyncClient(timeout=25, trust_env=trust_env) as c:
            r = await c.post(BASE + "/chat/completions", json=BODY, headers=HDR)
            print(name, "->", r.status_code, r.text[:120].replace("\n", " "))
    except Exception as e:
        print(name, "-> EXC:", type(e).__name__, str(e)[:150])

asyncio.run(test("via-system-proxy(trust_env=True) ", True))
asyncio.run(test("direct(trust_env=False)           ", False))
print("system proxy detected:", __import__("urllib.request", fromlist=["x"]).getproxies())
