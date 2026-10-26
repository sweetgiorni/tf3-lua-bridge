#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=2,<3", "httpx"]
# ///
"""End-to-end test of tools/lua_bridge_mcp.py over streamable HTTP.

Start the server first (against the offline harness or the real game), then:

    tools/tests/mcp_e2e.py [--url http://127.0.0.1:8765/mcp] [--token-file PATH] [--engine]

The harness: TEMP=<dir> mise exec -- lua tools/tests/bridge_harness.lua 60 &
             TF3_BRIDGE_DIR=<dir>/tf3_bridge LUA_BRIDGE_MCP_PORT=8799 tools/lua_bridge_mcp.py
"""
import argparse
import asyncio
import json
import os
import sys

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

fails = 0


def check(name, ok, detail=""):
    global fails
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"  -> {detail}"))
    if not ok:
        fails += 1


def text(res):
    return "".join(getattr(c, "text", "") for c in res.content)


async def session(url, token, fn):
    client = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
    async with streamable_http_client(url, http_client=client) as streams:
        async with ClientSession(streams[0], streams[1]) as s:
            await s.initialize()
            return await fn(s)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8765/mcp")
    ap.add_argument("--token-file", default=os.path.expanduser("~/.config/lua-bridge-mcp/token"))
    ap.add_argument("--engine", action="store_true", help="also test state=engine")
    a = ap.parse_args()
    token = open(a.token_file).read().strip()

    # auth
    async with httpx.AsyncClient() as h:
        r = await h.post(a.url, json={}, headers={"Accept": "application/json, text/event-stream"})
        check("no token -> 401", r.status_code == 401, r.status_code)
        r = await h.post(a.url, json={}, headers={"Authorization": "Bearer nope"})
        check("wrong token -> 401", r.status_code == 401, r.status_code)
        r = await h.post(a.url, json={}, headers={"Authorization": f"Bearer {token}", "Host": "evil.example"})
        check("foreign Host rejected", r.status_code in (400, 403, 421), r.status_code)

    async def basic(s):
        tools = {t.name for t in (await s.list_tools()).tools}
        check("tools listed", {"lua_eval", "lua_describe", "api_search", "game_log", "status", "reload_game"} <= tools,
              tools)
        st = json.loads(text(await s.call_tool("status", {})))
        check("status connected", st.get("connected") is True, st)
        r = await s.call_tool("lua_eval", {"code": "1 + 2"})
        check("eval expr", not r.is_error and '"result": 3' in text(r), text(r))
        r = await s.call_tool("lua_eval", {"code": "local t = {} for i=1,3 do t[i]=i*i end print('hi') return t"})
        check("eval statements + print", '"hi"' in text(r) and "9" in text(r), text(r))
        r = await s.call_tool("lua_eval", {"code": "error('boom')"})
        check("eval error -> isError", r.is_error and "boom" in text(r), text(r))
        r = await s.call_tool("lua_eval", {"code": "x = 41"})
        r = await s.call_tool("lua_eval", {"code": "x + 1"})
        check("gui state persists", '"result": 42' in text(r), text(r))
        r = await s.call_tool("lua_describe", {"path": "api.engine"})
        check("describe", not r.is_error and "members" in text(r), text(r)[:300])
        r = await s.call_tool("lua_describe", {"path": "a b; os.exit()"})
        check("describe rejects bad path", r.is_error, text(r))
        r = await s.call_tool("api_search", {"query": "lineSystem"})
        check("api_search", not r.is_error and "lineSystem" in text(r), text(r)[:300])
        r = await s.call_tool("game_log", {"lines": 3})
        check("game_log", not r.is_error, text(r)[:200])
        r = await s.call_tool("reload_game", {})
        check("reload_game refuses without confirm", r.is_error, text(r))
        if a.engine:
            r = await s.call_tool("lua_eval", {"code": "return 6 * 7", "state": "engine"})
            check("engine eval", '"result": 42' in text(r), text(r))

    await session(a.url, token, basic)

    # concurrent sessions must never see each other's answers
    async def worker(n):
        async def go(s):
            for i in range(10):
                r = await s.call_tool("lua_eval", {"code": f"return {n * 1000 + i}"})
                if f'"result": {n * 1000 + i}' not in text(r):
                    return text(r)
            return None
        return await session(a.url, token, go)

    res = await asyncio.gather(*(worker(n) for n in range(1, 5)))
    check("4 concurrent sessions x 10 calls", all(r is None for r in res), res)

    # many parallel calls on one session
    async def burst(s):
        rs = await asyncio.gather(*(s.call_tool("lua_eval", {"code": f"return {i}"}) for i in range(30)))
        return all(f'"result": {i}' in text(r) for i, r in enumerate(rs))

    check("30 parallel calls, one session", await session(a.url, token, burst))
    print("FAILED" if fails else "all passed")
    return 1 if fails else 0


sys.exit(asyncio.run(main()))
