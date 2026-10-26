# Lua Bridge

A Transport Fever 3 mod that lets external programs run Lua inside the running game, plus a command line client and an MCP server so AI assistants such as Claude Code can inspect the game and help you develop mods.

With it you can, without leaving your editor:

- evaluate Lua in the game's GUI state or in the simulation (engine) state and get the result back as JSON,
- explore the live API (`api.engine.system...`) and its signatures,
- search the shipped API type definitions,
- read the game log and reload the current save after changing a mod.

The mod only provides the connection. Nothing happens until a client connects.

**Windows only.** The mod and the tools talk through files in `%TEMP%\tf3_bridge`; the mod stays idle on other platforms.

## Security

While the mod is enabled, any program running as you that can write to `%TEMP%\tf3_bridge` can run Lua in your game. Enable the mod only while you are developing.

The mod option "Lua access for requests" controls what that Lua can reach:

- **Restricted** (default): hides `require`, `io`, `os.getenv`, `load` and `debug`. This stops accidents; it is not a security boundary, because the game API itself can still change or delete things (savegames, for example).
- **Full**: no restrictions on that Lua.

The MCP server listens on `127.0.0.1` only and requires a bearer token.

## Install

1. Subscribe to Lua Bridge on mod.io (or copy this repo's `mod.json`, `strings.json`, `_metadata/` and `content/` into the game's `mods/release/lua_bridge` folder) and enable it in the game's mod manager.
2. Load a save. The mod starts watching for a client.

## Use with Claude Code

Requirements: Python 3.10+ and [uv](https://docs.astral.sh/uv/) (it installs the server's dependencies on first run).

Register the server once. This prints the exact `claude mcp add` command for your machine; run it:

```
uv run tools/lua_bridge_mcp.py --register
```

After that Claude Code starts everything by itself. Each Claude session runs a small proxy; the first one starts a shared background server (the only program that talks to the game), and later sessions reuse it. That server exits after 30 minutes without requests. Without `uv`: `pip install "mcp>=2,<3" uvicorn`, and `--register` prints a `python` command instead. Its log is `~/.config/lua-bridge-mcp/server.log`.

Tools it offers:

| Tool | What it does |
|---|---|
| `lua_eval` | Run Lua in the `gui` state (about 20 ms, variables persist) or the `engine` state (about 0.4 s, variables do not persist reliably) |
| `lua_describe` | List what exists at a path such as `api.engine.system.lineSystem`, with native function signatures |
| `api_search` | Search the shipped `.d.tl` API definitions and their comments |
| `game_log` | Tail or grep the game's log |
| `status` | Connection state, access level, game build |
| `reload_game` | Destructive: refresh mods and reload the current save |

If your Steam library is not in `C:\Program Files (x86)\Steam`, set `TF3_INSTALL` to the game folder.

## Command line

```
python tools/bridge_cli.py eval "api.engine.util.getPlayer()"
python tools/bridge_cli.py ping
```

The MCP server and the CLI both take the bridge lock, so the CLI reports "bridge in use" while the server is running (up to 30 minutes after its last use).

## Limits

- Lua is 5.2. Change the game only through `api.cmd`; commands run on the next simulation step.
- Engine-state evals run in whichever of the game's Lua states is free, so do not rely on variables surviving between calls.
- Results are capped (1 MiB for the GUI state, 256 KB for the engine state) and large values are cut by depth and item count.

The wire protocol is described in [PROTOCOL.md](PROTOCOL.md). Development notes (tests, packaging, cover image) are in [CONTRIBUTING.md](CONTRIBUTING.md).

## License

GPL-3.0, see [LICENSE](LICENSE).
