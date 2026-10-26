#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=2,<3", "uvicorn"]
# ///
"""MCP server for the Lua Bridge mod (mods/lua-bridge; protocol: PROTOCOL.md).

A shared daemon is the only holder of the bridge lock and the only writer to the game; every Claude session talks to it
over HTTP, and the sessions are told apart by the opaque `context` ({"session", "seq"}) that the mod echoes in each
response. Claude Code does not manage a daemon, so it runs this script as a stdio proxy (`--stdio`) that finds the
daemon, starts it if needed, and forwards the tool calls.

    tools/lua_bridge_mcp.py --register      print the `claude mcp add` command for the stdio proxy (the normal setup)
    tools/lua_bridge_mcp.py --stdio         the proxy itself (what Claude Code launches)
    tools/lua_bridge_mcp.py                 run the daemon in the foreground on http://127.0.0.1:8765/mcp
                                            (LUA_BRIDGE_MCP_PORT to change; --idle-exit MINUTES to quit when unused)
    tools/lua_bridge_mcp.py --register-http print the `claude mcp add` command for a daemon you run yourself

Security: the game's Lua runs with the rights of the game process, so this server is a remote-execution endpoint for
everything that can reach it. It listens on 127.0.0.1 only, rejects foreign Host/Origin headers (DNS rebinding) and
requires `Authorization: Bearer <token>`, where the token lives in a 0600 file (default
~/.config/lua-bridge-mcp/token).
"""
from __future__ import annotations

import asyncio
import atexit
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bridge_cli import BIN, default_dir  # noqa: E402

from mcp.server.mcpserver import Context, MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402

HOST = "127.0.0.1"
PORT = int(os.environ.get("LUA_BRIDGE_MCP_PORT", "8765"))
TOKEN_FILE = Path(os.environ.get("LUA_BRIDGE_MCP_TOKEN_FILE", "~/.config/lua-bridge-mcp/token")).expanduser()
if os.name == "nt":
    STEAM = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Steam"
else:  # WSL
    STEAM = Path("/mnt/c/Program Files (x86)/Steam")
GAME_DIR = Path(os.environ.get("TF3_INSTALL", STEAM / "steamapps" / "common" / "Transport Fever 3"))


def find_game_log() -> Path:
    """LUA_BRIDGE_GAME_LOG, else the newest Steam userdata crash_dump/stdout.txt next to the game install."""
    if os.environ.get("LUA_BRIDGE_GAME_LOG"):
        return Path(os.environ["LUA_BRIDGE_GAME_LOG"])
    steam = GAME_DIR.parents[2]  # <steam>/steamapps/common/<game>
    found = sorted((steam / "userdata").glob("*/*/local/crash_dump/stdout.txt"), key=lambda p: p.stat().st_mtime)
    return found[-1] if found else steam / "userdata" / "stdout.txt"


GAME_LOG = find_game_log()


class BridgeUnavailable(Exception):
    pass


# ------------------------------------------------------------------------------------------------------------------
# connection to the game

class GameBridge:
    """Async client for the file protocol. Many tool calls can be in flight; each carries its own context."""

    def __init__(self, directory: str):
        self.dir = directory
        self.lock_path = os.path.join(directory, "bridge.lock")
        self._in_path = os.path.join(directory, "in.jsonl")
        self._out_path = os.path.join(directory, "out.jsonl")
        self._fd_in: int | None = None
        self._fd_out: int | None = None
        self._buf = b""
        self._waiters: dict[tuple, asyncio.Future] = {}
        self._seqs: dict[str, int] = {}
        self._connect_lock = asyncio.Lock()
        self._reader: asyncio.Task | None = None
        self.info: dict | None = None  # the mod's hello reply while we believe the connection is good

    # -- the bridge lock: one server per game ---------------------------------------------------------------------
    def acquire_lock(self) -> None:
        os.makedirs(self.dir, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | BIN)
            except FileExistsError:
                try:
                    holder = open(self.lock_path).read()
                except OSError:
                    holder = ""
                m = re.search(r"pid=(\d+)", holder)
                if m and _alive(int(m.group(1))):
                    raise RuntimeError(f"another process (pid {m.group(1)}) holds {self.lock_path}")
                try:  # stale lock of a dead process
                    os.remove(self.lock_path)
                except FileNotFoundError:
                    pass
                continue
            os.write(fd, f"lua-bridge-mcp pid={os.getpid()}\n".encode())
            os.close(fd)
            atexit.register(self.release_lock)
            return
        raise RuntimeError(f"could not take {self.lock_path}")

    def release_lock(self) -> None:
        try:
            if f"pid={os.getpid()}" in open(self.lock_path).read():
                os.remove(self.lock_path)
        except OSError:
            pass

    # -- connection -----------------------------------------------------------------------------------------------
    def _open_files(self) -> None:
        if self._fd_in is None:
            self._fd_in = os.open(self._in_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | BIN)
            self._fd_out = os.open(self._out_path, os.O_RDONLY | os.O_CREAT | BIN)
            os.lseek(self._fd_out, 0, os.SEEK_END)  # only answers written from now on are ours
            os.write(self._fd_in, b"\n")  # closes off a torn line left by a crashed writer
        if self._reader is None or self._reader.done():
            self._reader = asyncio.get_running_loop().create_task(self._read_loop())

    async def _read_loop(self) -> None:
        while True:
            chunk = os.read(self._fd_out, 1 << 20)
            if not chunk:
                await asyncio.sleep(0.01)
                continue
            self._buf += chunk
            *lines, self._buf = self._buf.split(b"\n")
            for line in lines:
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                ctx = msg.get("context") if isinstance(msg, dict) else None
                if isinstance(ctx, dict):
                    waiter = self._waiters.pop((ctx.get("session"), ctx.get("seq")), None)
                    if waiter and not waiter.done():
                        waiter.set_result(msg)

    def _send(self, obj: dict) -> None:
        os.write(self._fd_in, (json.dumps(obj, separators=(",", ":")) + "\n").encode())

    def _next_ctx(self, session: str) -> dict:
        self._seqs[session] = self._seqs.get(session, 0) + 1
        return {"session": session, "seq": self._seqs[session]}

    async def _ask(self, op: str, session: str, timeout: float, **fields) -> dict:
        ctx = self._next_ctx(session)
        waiter = asyncio.get_running_loop().create_future()
        key = (ctx["session"], ctx["seq"])
        self._waiters[key] = waiter
        self._send({"op": op, "context": ctx, **fields})
        try:
            return await asyncio.wait_for(waiter, timeout)
        except asyncio.TimeoutError:
            self._waiters.pop(key, None)
            raise

    async def connect(self, timeout: float = 10.0) -> dict:
        async with self._connect_lock:
            if self.info is not None:
                return self.info
            self._open_files()
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:  # hello is idempotent: resend until the mod answers
                try:
                    self.info = await self._ask("hello", "_server", 1.0)
                    return self.info
                except asyncio.TimeoutError:
                    pass
            raise BridgeUnavailable(
                "no answer from the game: is it running with a save loaded and the Lua Bridge mod enabled?")

    async def request(self, op: str, session: str, timeout: float = 30.0, **fields) -> dict:
        await self.connect()
        try:
            return await self._ask(op, session, timeout, **fields)
        except asyncio.TimeoutError:
            self.info = None  # the game may have been reloaded: handshake again on the next call
            raise BridgeUnavailable(
                f"no answer within {timeout:g}s (the game may be busy, paused in a menu or reloading)") from None


def _alive(pid: int) -> bool:
    if os.name == "nt":  # os.kill would terminate the process here
        import ctypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: it exists
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
        k32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


bridge = GameBridge(default_dir())

# ------------------------------------------------------------------------------------------------------------------
# helpers

def session_of(ctx: Context) -> str:
    try:
        sid = (ctx.headers or {}).get("mcp-session-id")
    except Exception:
        sid = None
    return (sid or f"conn{id(ctx.session) & 0xFFFFFF:x}")[:40]


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [{len(text) - limit} more characters cut; raise max_chars or narrow the query]"


def format_eval(resp: dict, max_chars: int) -> str:
    if not resp.get("ok"):
        kind = resp.get("kind") or "request"
        lines = [f"{kind} error: {resp.get('error')}"]
        if resp.get("traceback"):
            lines.append(str(resp["traceback"]).strip())
        if resp.get("log"):
            lines.append("print output:\n" + "\n".join(resp["log"]))
        raise ToolError("\n".join(lines))
    out = {"result": resp.get("result")}
    for k in ("log", "truncated", "engineState"):
        if resp.get(k) is not None:
            out[k] = resp[k]
    out["ms"] = resp.get("ms")
    return clip(json.dumps(out, ensure_ascii=False, indent=1), max_chars)


async def run_eval(session: str, code: str, state: str, max_depth: int, max_items: int, timeout: float) -> dict:
    try:
        return await bridge.request("eval", session, timeout, code=code, state=state, maxDepth=max_depth,
                                    maxItems=max_items)
    except BridgeUnavailable as e:
        raise ToolError(str(e)) from None


# ------------------------------------------------------------------------------------------------------------------
# tools

mcp = MCPServer(
    "lua-bridge",
    instructions=(
        "Runs Lua inside the running Transport Fever 3 game, for developing mods and inspecting the live game. "
        "lua_eval with state='gui' (default) takes about one frame and can read every engine system and send commands "
        "(api.cmd, executed on the next simulation step); state='engine' runs inside the simulation's Lua states "
        "(about 0.4 s per call, and variables do NOT persist reliably between calls). Use lua_describe and api_search "
        "to find what exists before guessing. The game must have a save loaded with the Lua Bridge mod enabled."),
)


@mcp.tool()
async def lua_eval(code: str, ctx: Context, state: Literal["gui", "engine"] = "gui", max_depth: int = 6,
                   max_items: int = 500, timeout: float = 30.0, max_chars: int = 40000) -> str:
    """Run Lua in the running game and return the result as JSON.

    `code` is tried as an expression (`return <code>`) first, then as statements, so both `api.engine.util.getPlayer()`
    and `local n = 0 for _ in pairs(t) do n = n + 1 end return n` work. Several return values come back as an array.
    Output of `print` is captured in `log`. Tables are encoded to `max_depth` levels and `max_items` entries each;
    functions and userdata become strings. Lua 5.2; mutate the game only through api.cmd.

    state='gui' (default): the persistent GUI state (~20 ms). Variables you assign persist between calls. It can
    read all of api.engine and send commands.
    state='engine': inside the simulation's Lua states (~0.4 s). Which of several states runs your code varies, so do
    not rely on variables surviving between calls; results are capped at 256 KB.
    The environment is Restricted or Full depending on the mod's setting (see status)."""
    resp = await run_eval(session_of(ctx), code, state, max_depth, max_items, timeout)
    return format_eval(resp, max_chars)


_DESCRIBE_LUA = r'''
local path = %s
local v = _G
for part in path:gmatch("[^.]+") do
  local tv = type(v)
  if tv ~= "table" and tv ~= "userdata" then return { error = "cannot index a " .. tv .. " before '" .. part .. "'" } end
  local ok, nv = pcall(function() return v[part] end)
  if not ok then return { error = tostring(nv) } end
  v = nv
end
local out = { path = path, type = type(v) }
local ok, doc = pcall(function() return v.__doc__ end)
if ok and type(doc) == "string" then out.doc = doc end
if type(v) == "table" or type(v) == "userdata" then
  local members = {}
  pcall(function()
    for k, val in pairs(v) do
      if type(k) == "string" and k:sub(1, 2) ~= "__" then
        local m = { name = k, type = type(val) }
        local ok2, d = pcall(function() return val.__doc__ end)
        if ok2 and type(d) == "string" then m.doc = d end
        members[#members + 1] = m
      end
    end
  end)
  table.sort(members, function(a, b) return a.name < b.name end)
  out.count = #members
  if #members > %d then
    for i = #members, %d + 1, -1 do members[i] = nil end
    out.cut = true
  end
  out.members = members
elseif type(v) ~= "function" then
  out.value = tostring(v)
end
return out
'''


@mcp.tool()
async def lua_describe(path: str, ctx: Context, limit: int = 300, max_chars: int = 40000) -> str:
    """List what exists at a dotted path in the game's Lua, with native function signatures.

    Examples: 'api', 'api.engine', 'api.engine.system.lineSystem', 'api.cmd', 'api.engine.system.lineSystem.getLinesForPlayer'.
    For a table or userdata it lists member names with their types, and for native functions the runtime signature
    (the `__doc__` string, e.g. '(Entity playerEntity) -> Vector[Entity]'). Complements api_search, which reads the
    shipped .d.tl type definitions (with their comments)."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*", path):
        raise ToolError("path must look like api.engine.system (dotted identifiers)")
    code = _DESCRIBE_LUA % (json.dumps(path), limit, limit)
    resp = await run_eval(session_of(ctx), code, "gui", 4, limit + 5, 30.0)
    return format_eval(resp, max_chars)


class ApiIndex:
    """Searchable index of the shipped .d.tl type definitions: record members with the comments above them."""

    RECORD = re.compile(r"^(\s*)(?:global\s+)?(?:record|interface|enum)\s+([A-Za-z_][\w.]*)")
    MEMBER = re.compile(r"^\s*([A-Za-z_]\w*)\s*:\s*(.+?)\s*$")

    def __init__(self) -> None:
        self.entries: list[dict] = []
        self.built = False

    def build(self) -> None:
        for root in (GAME_DIR / "api" / "tealdef", GAME_DIR / "base" / "tealdef"):
            for f in sorted(root.rglob("*.d.tl")):
                try:
                    lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    continue
                rel = f.relative_to(GAME_DIR).as_posix()
                stack: list[tuple[int, str]] = []
                doc: list[str] = []
                for n, raw in enumerate(lines, 1):
                    stripped = raw.strip()
                    indent = len(raw) - len(raw.lstrip())
                    if stripped.startswith("--"):
                        doc.append(stripped.lstrip("-").strip())
                        continue
                    m = self.RECORD.match(raw)
                    if m:
                        stack.append((len(m.group(1)), m.group(2)))
                        self._add("type", m.group(2), "", stripped, doc, rel, n)
                    elif stripped == "end":
                        if stack and stack[-1][0] >= indent:
                            stack.pop()
                    else:
                        mm = self.MEMBER.match(raw)
                        if mm and stack:
                            self._add("member", mm.group(1), ".".join(name for _, name in stack), mm.group(2), doc,
                                      rel, n)
                    doc = []
        self.built = True

    def _add(self, kind, name, container, sig, doc, rel, line) -> None:
        text = " ".join(doc)
        self.entries.append({"kind": kind, "name": name, "container": container, "sig": sig[:300], "doc": text[:600],
                             "where": f"{rel}:{line}", "hay": f"{container}.{name} {sig} {text}".lower(),
                             "name_l": name.lower(), "cont_l": container.lower()})

    def search(self, query: str, limit: int) -> list[dict]:
        if not self.built:
            self.build()
        toks = [t for t in re.split(r"[\s.]+", query.lower()) if t]
        scored = []
        for e in self.entries:
            if not all(t in e["hay"] for t in toks):
                continue
            score = sum((6 if t == e["name_l"] else 3 if t in e["name_l"] else 2 if t in e["cont_l"] else 1)
                        for t in toks)
            scored.append((score, e))
        scored.sort(key=lambda x: (-x[0], len(x[1]["name"]), x[1]["where"]))
        return [e for _, e in scored[:limit]]


api_index = ApiIndex()


@mcp.tool()
async def api_search(query: str, limit: int = 15, max_chars: int = 30000) -> str:
    """Search the game's shipped Lua API type definitions (.d.tl files) by words in names, containing types and comments.

    Every word must match somewhere in 'Container.member signature comment'. Example: 'lineSystem', 'getLinesForPlayer',
    'cargo load', 'sendCommand'. Returns the declaration, its doc comment and the file:line (relative to the game
    install). Use lua_describe for the live runtime view and signatures of native functions."""
    if not GAME_DIR.exists():
        raise ToolError(f"game directory not found: {GAME_DIR} (set TF3_INSTALL)")
    hits = await asyncio.get_running_loop().run_in_executor(None, api_index.search, query, max(1, min(limit, 100)))
    if not hits:
        return f"no definitions match '{query}'"
    out = []
    for e in hits:
        head = f"{e['container']}.{e['name']}" if e["container"] else e["name"]
        out.append(f"{head}  [{e['where']}]\n  {e['sig']}" + (f"\n  -- {e['doc']}" if e["doc"] else ""))
    return clip("\n".join(out), max_chars)


@mcp.tool()
async def game_log(lines: int = 100, pattern: str | None = None, max_chars: int = 30000) -> str:
    """Tail of the game's log (stdout.txt): mod `debugPrint` output, Lua errors, load messages.

    `pattern` is a case-insensitive regular expression; only matching lines are returned (the last `lines` of them).
    Mod output carries a prefix such as [lua_bridge] or [MoveIt]."""
    try:
        size = GAME_LOG.stat().st_size
        with open(GAME_LOG, "rb") as f:
            f.seek(max(0, size - 4 * 1024 * 1024))
            data = f.read().decode("utf-8", errors="replace")
    except OSError as e:
        raise ToolError(f"cannot read the game log {GAME_LOG}: {e}") from None
    rows = data.splitlines()
    if pattern:
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            raise ToolError(f"bad pattern: {e}") from None
        rows = [r for r in rows if rx.search(r)]
    rows = [r[:600] for r in rows[-max(1, lines):]]
    return clip("\n".join(rows) or "(no matching lines)", max_chars)


@mcp.tool()
async def status(ctx: Context) -> str:
    """Connection state: whether the game answers, the mod's run id, access level (restricted or full), available
    states and game build."""
    bridge.info = None  # a fresh handshake: the cached one outlives a crashed or restarted game
    try:
        info = await bridge.connect(timeout=4.0)
    except BridgeUnavailable as e:
        return json.dumps({"connected": False, "reason": str(e), "bridge_dir": bridge.dir})
    return json.dumps({"connected": True, "run": info.get("run"), "access": info.get("access"),
                       "states": info.get("states"), "build": info.get("build"), "protocol": info.get("version"),
                       "bridge_dir": bridge.dir}, indent=1)


_LOAD_SAVE_LUA = """local g = app.SaveGameNamespace.getSavegame()
local l = app.findAllSavegames(g)
for i = 1, #l do
  if l[i].saveName == %s then
    local id = api.type.SavegameId.new()
    id.path = l[i].path id.saveGameName = l[i].saveName id.saveGameNamespace = g
    app.loadGame(id, false)
    return "loading"
  end
end
return "not found"
"""


@mcp.tool()
async def reload_game(ctx: Context, confirm: bool = False, save_name: str | None = None, wait: float = 150.0) -> str:
    """DESTRUCTIVE: reload the mods and the current save, discarding unsaved game progress.

    Runs app.refreshMods() and then loads the save again, which is how changed mod scripts take effect (a file ADDED
    to a loaded mod only appears after a full game restart). `save_name` defaults to the save named in the game log's
    last 'Loading game from file ...' line. Pass confirm=true to proceed. Waits until the bridge answers again from a
    fresh mod instance (about 20-30 s) and returns the new mod state."""
    if not confirm:
        raise ToolError("reload_game discards unsaved progress; call it again with confirm=true")
    name = save_name
    if not name:
        try:
            tail = GAME_LOG.read_bytes()[-4 * 1024 * 1024:].decode("utf-8", errors="replace")
        except OSError as e:
            raise ToolError(f"cannot read the game log to find the loaded save: {e}") from None
        found = re.findall(r"Loading game from file (.+?)\s*$", tail, flags=re.MULTILINE)
        if not found:
            raise ToolError("could not tell which save is loaded; pass save_name")
        name = found[-1]
    session = session_of(ctx)
    try:
        before = (await bridge.connect()).get("run")
    except BridgeUnavailable as e:
        raise ToolError(str(e)) from None
    await run_eval(session, "app.refreshMods() return 1", "gui", 2, 10, 30.0)
    await asyncio.sleep(3)
    try:
        r = await run_eval(session, _LOAD_SAVE_LUA % json.dumps(name), "gui", 2, 10, 15.0)
    except ToolError:
        r = None  # the GUI state may already be gone
    if r is not None and r.get("ok") and r.get("result") == "not found":
        raise ToolError(f"no save named {name!r} in the savegame namespace")
    bridge.info = None
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        await asyncio.sleep(3)
        try:
            info = await bridge.connect(timeout=4.0)
        except BridgeUnavailable:
            continue
        if info.get("run") != before:
            await asyncio.sleep(15)  # let the engine states register and subscribe to events
            return json.dumps({"reloaded": name, "run": info.get("run"), "access": info.get("access")})
        bridge.info = None
    raise ToolError("the game did not come back within the wait time (is it still loading?)")


# ------------------------------------------------------------------------------------------------------------------
# HTTP with a bearer token

def load_token() -> str:
    if not TOKEN_FILE.exists():
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, (secrets.token_urlsafe(32) + "\n").encode())
        os.close(fd)
    return TOKEN_FILE.read_text().strip()


class BearerAuth:
    """Pure ASGI wrapper: every HTTP request must carry the bearer token."""

    last_request = time.monotonic()  # for --idle-exit

    def __init__(self, app, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            BearerAuth.last_request = time.monotonic()
            given = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(given, self.expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain"), (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
        await self.app(scope, receive, send)


def build_app(token: str):
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[f"{HOST}:{PORT}", f"localhost:{PORT}"],
        allowed_origins=[],
    )
    return BearerAuth(mcp.streamable_http_app(transport_security=security, host=HOST), token)


async def serve(idle_exit_minutes: float = 0) -> None:
    import uvicorn

    token = load_token()
    bridge.acquire_lock()
    server = uvicorn.Server(uvicorn.Config(build_app(token), host=HOST, port=PORT, log_level="warning"))
    print(f"lua-bridge MCP server on http://{HOST}:{PORT}/mcp (bridge folder {bridge.dir}, token in {TOKEN_FILE})",
          flush=True)

    async def watch_idle() -> None:
        while not server.should_exit:
            await asyncio.sleep(min(15, idle_exit_minutes * 30))
            if time.monotonic() - BearerAuth.last_request > idle_exit_minutes * 60:
                print("idle, exiting", flush=True)
                server.should_exit = True

    watcher = asyncio.get_running_loop().create_task(watch_idle()) if idle_exit_minutes else None
    try:
        await server.serve()
    finally:
        if watcher:
            watcher.cancel()


# ------------------------------------------------------------------------------------------------------------------
# stdio proxy: what Claude Code launches. It owns nothing but a connection to the shared daemon.

async def daemon_up() -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(HOST, PORT), 1.0)
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    return True


def spawn_daemon() -> None:
    """Start the daemon detached from this process, so it outlives the Claude session that needed it."""
    log = open(TOKEN_FILE.parent / "server.log", "ab")
    flags = {}
    if os.name == "nt":
        flags["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED_PROCESS | NEW_PROCESS_GROUP | NO_WINDOW
    else:
        flags["start_new_session"] = True
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--idle-exit", "30"], stdin=subprocess.DEVNULL,
                     stdout=log, stderr=log, close_fds=True, **flags)


async def ensure_daemon() -> None:
    if await daemon_up():
        return
    spawn_daemon()  # if two proxies race, the loser exits on the bridge lock or the port
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        await asyncio.sleep(0.3)
        if await daemon_up():
            return
    raise RuntimeError(f"the Lua Bridge server did not start; see {TOKEN_FILE.parent / 'server.log'}")


class Upstream:
    """The proxy's HTTP session with the daemon. One owner task holds the connection open and reopens it (starting the
    daemon again if needed) after a failure, so the daemon sees one MCP session per Claude session."""

    def __init__(self, token: str):
        self.token = token
        self.session = None
        self.instructions: str | None = None
        self.ready = asyncio.Event()
        self.broken = asyncio.Event()
        self.task: asyncio.Task | None = None

    def start(self) -> None:
        self.task = asyncio.get_running_loop().create_task(self._own())

    async def _own(self) -> None:
        from mcp import ClientSession
        from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

        while True:
            try:
                await ensure_daemon()
                client = create_mcp_http_client(headers={"Authorization": f"Bearer {self.token}"})
                async with streamable_http_client(f"http://{HOST}:{PORT}/mcp", http_client=client) as streams:
                    async with ClientSession(streams[0], streams[1]) as session:
                        init = await session.initialize()
                        self.instructions = init.instructions
                        self.session = session
                        self.ready.set()
                        await self.broken.wait()
            except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                raise
            except BaseException as e:  # includes exception groups from the transport's task group
                print(f"lua-bridge proxy: upstream error: {e!r}", file=sys.stderr, flush=True)
            finally:
                self.session = None
                self.ready.clear()
                self.broken.clear()
            await asyncio.sleep(1)

    async def call(self, fn):
        for attempt in (0, 1):
            await asyncio.wait_for(self.ready.wait(), 40)
            try:
                return await fn(self.session)
            except Exception:
                self.ready.clear()  # the retry must wait for the new session, not reuse this one
                self.broken.set()  # reconnect, then retry once
                if attempt:
                    raise


async def serve_stdio() -> None:
    from mcp import types
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server

    token = load_token()
    up = Upstream(token)
    up.start()
    await asyncio.wait_for(up.ready.wait(), 40)

    async def on_list_tools(ctx, params):
        return types.ListToolsResult(tools=(await up.call(lambda s: s.list_tools())).tools)

    async def on_call_tool(ctx, params):
        return await up.call(lambda s: s.call_tool(params.name, params.arguments))

    server = Server("lua-bridge", instructions=up.instructions, on_list_tools=on_list_tools, on_call_tool=on_call_tool)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def register_command(http: bool) -> str:
    if http:
        return (f'claude mcp add --transport http --scope user lua-bridge http://{HOST}:{PORT}/mcp '
                f'--header "Authorization: Bearer {load_token()}"')
    script = str(Path(__file__).resolve())
    argv = ["uv", "run", "--script", script, "--stdio"] if shutil.which("uv") else [sys.executable, script, "--stdio"]
    quoted = subprocess.list2cmdline(argv) if os.name == "nt" else " ".join(shlex_quote(a) for a in argv)
    return f"claude mcp add --scope user lua-bridge -- {quoted}"


def shlex_quote(a: str) -> str:
    import shlex

    return shlex.quote(a)


def main() -> int:
    args = sys.argv[1:]
    if "--register" in args or "--register-http" in args:
        load_token()
        print(register_command("--register-http" in args))
        return 0
    try:
        if "--stdio" in args:
            load_token()
            asyncio.run(serve_stdio())
        else:
            idle = float(args[args.index("--idle-exit") + 1]) if "--idle-exit" in args else 0
            asyncio.run(serve(idle))
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
