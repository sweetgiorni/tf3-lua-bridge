#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=2,<3"]
# ///
"""End-to-end test of the stdio proxy (`lua_bridge_mcp.py --stdio`) and the daemon it manages.

Needs a bridge to talk to: the offline harness (TEMP=<dir> lua tools/tests/bridge_harness.lua 120 &) or the real game
with no other server running. Everything runs on a private port and token file, so it does not touch a normal install.

    tools/tests/shim_e2e.py [--bridge-dir DIR] [--port 8799]
"""
import argparse
import asyncio
import json
import os
import signal
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = Path(__file__).resolve().parent.parent / "lua_bridge_mcp.py"
fails = 0


def check(name, ok, detail=""):
    global fails
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"  -> {detail}"))
    if not ok:
        fails += 1


def text(res):
    return "".join(getattr(c, "text", "") for c in res.content)


def lock_pid(bridge_dir):
    try:
        return int(open(os.path.join(bridge_dir, "bridge.lock")).read().split("pid=")[1].split()[0])
    except (OSError, IndexError, ValueError):
        return None


def alive(pid):
    if pid is None:
        return False
    if os.name == "nt":
        import ctypes

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k.OpenProcess(0x1000, False, pid)
        if not h:
            return False
        code = ctypes.c_ulong()
        k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return code.value == 259
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def kill(pid):
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


async def wait_for(cond, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.3)
    return cond()


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bridge-dir", required=True)
    ap.add_argument("--port", default="8799")
    a = ap.parse_args()
    tmp = tempfile.mkdtemp(prefix="lb-shim-")
    env = {**os.environ, "TF3_BRIDGE_DIR": a.bridge_dir, "LUA_BRIDGE_MCP_PORT": a.port,
           "LUA_BRIDGE_MCP_TOKEN_FILE": os.path.join(tmp, "token")}
    params = StdioServerParameters(command=sys.executable, args=[str(SERVER), "--stdio"], env=env)

    @asynccontextmanager
    async def proxy():
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                yield s

    check("no daemon before the first proxy", lock_pid(a.bridge_dir) is None)

    async with proxy() as one:
        names = {t.name for t in (await one.list_tools()).tools}
        check("proxy lists the daemon's tools", {"lua_eval", "status", "lua_describe", "api_search"} <= names, names)
        r = await one.call_tool("lua_eval", {"code": "1 + 2"})
        check("proxy forwards lua_eval", not r.is_error and '"result": 3' in text(r), text(r))
        daemon = lock_pid(a.bridge_dir)
        check("proxy started the daemon", alive(daemon), daemon)

        async with proxy() as two:
            r2 = await two.call_tool("lua_eval", {"code": "return 'two'"})
            r1 = await one.call_tool("lua_eval", {"code": "return 'one'"})
            check("two proxies share one daemon", lock_pid(a.bridge_dir) == daemon and "two" in text(r2) and "one" in text(r1))
        r = await one.call_tool("lua_eval", {"code": "error('boom')"})
        check("tool errors pass through", r.is_error and "boom" in text(r), text(r))

    check("daemon outlives its proxies", alive(daemon))

    async with proxy() as three:
        kill(daemon)
        check("daemon stopped", await wait_for(lambda: not alive(daemon), 15))
        r = await three.call_tool("lua_eval", {"code": "return 'back'"})
        new = lock_pid(a.bridge_dir)
        check("proxy respawns a killed daemon mid-session", "back" in text(r) and alive(new) and new != daemon, (text(r), new))
        daemon = new
    kill(daemon)
    check("cleanup", await wait_for(lambda: not alive(daemon), 15))

    # idle exit: a daemon started with --idle-exit quits when nobody uses it
    proc = await asyncio.create_subprocess_exec(sys.executable, str(SERVER), "--idle-exit", "0.1", env=env,
                                                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    check("idle daemon exits by itself", await asyncio.wait_for(proc.wait(), 60) == 0)
    print("FAILED" if fails else "all passed")
    return 1 if fails else 0


sys.exit(asyncio.run(main()))
