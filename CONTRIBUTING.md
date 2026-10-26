# Developing Lua Bridge

## Layout

- `mod.json`, `strings.json`, `_metadata/`, `content/`: the mod itself; with `LICENSE`, the only files that are published.
- `tools/`: the client, MCP server, tests, packaging and cover scripts. Not part of the package.
- `PROTOCOL.md`: the wire protocol.

## Tests

Run from the repository root. The offline tests need Lua 5.4 and Python 3.

- `lua tools/tests/bridge_json_test.lua`: the JSON module.
- `tools/tests/bridge_harness.lua`: runs the real mod script with stubbed game globals and a simulated frame clock. Start it with `TEMP=<dir> lua tools/tests/bridge_harness.lua 30 [full] &`, then run `python tools/tests/bridge_e2e.py --dir <dir>/tf3_bridge [--full]`.
- `tools/tests/shim_e2e.py --bridge-dir <dir>/tf3_bridge`: the stdio proxy and the daemon it starts (two proxies, respawn after a kill, idle exit). Uses its own port and token file.
- `tools/tests/mcp_e2e.py [--url ...] [--engine]`: the daemon over HTTP. For the harness, start the server with `TF3_BRIDGE_DIR=<dir>/tf3_bridge LUA_BRIDGE_MCP_PORT=8799`.

`bridge_e2e.py` and `mcp_e2e.py` also work against the real game (leave out `--dir` and the harness).

## Packaging

`tools/package.sh [staging-area]` (needs bash and rsync) copies the mod (`mod.json`, `strings.json`, `LICENSE`, `_metadata/`, `content/`) to `<staging_area>/lua_bridge/` and verifies the copy. The default staging area is `<Steam>/userdata/*/3493540/local/staging_area`; override it with the argument or `TF3_STAGING_AREA`. Everything else in the repository stays out, and the script refuses to package `.py`, `.sh`, `.bat` or `.exe` files under `content/` or `_metadata/`. Re-running updates the copy and keeps `_metadata/mod.io_fileid.txt`.

## Cover image

`_metadata/0.png` (1920x1080) is the cover; the game picks it up by file name. `tools/lua_bridge_cover.py [scale]` generates it with Pillow from `tools/background.png`, and needs the Inter font under `/usr/share/fonts/opentype/inter/`. It writes `lua_bridge_cover_<scale>x.png` to the working directory.
