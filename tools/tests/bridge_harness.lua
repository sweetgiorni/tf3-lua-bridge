-- Runs the real bridge.script.lua outside the game with stubbed game globals, so the protocol can be tested offline.
-- Usage: TEMP=<dir> mise exec -- lua tools/tests/bridge_harness.lua <seconds> [full|restricted] [Linux|Console]
-- (default: simulates Windows paths; Linux uses real / paths; Console must stay idle)
local seconds = tonumber(arg[1]) or 30
local base = "content/lua_bridge/"
package.path = base .. "?.lua;" .. package.path
package.config = (arg[3] == "Linux" and "/" or "\\") .. "\n;\n?\n!\n-\n" -- what the game's Lua reports; the script only looks at the separator
local json = dofile(base .. "bridge_json.lua")
local realIo = io
local ioLib = setmetatable({ -- the script builds Windows paths (see package.config above); this machine wants "/"
	open = function(path, mode) return realIo.open((path:gsub("\\", "/")), mode) end,
}, { __index = realIo })
getOwningModId = function() return "lua_bridge" end
ug_require = function(path) return json end
debugPrint = function(...) print(...) end
getBuildVersion = function() return "harness" end
local now = 0 -- simulated application time, one 4 ms frame per loop (os.clock is CPU time and would stand still)
api = {
	util = { getApplicationTime = function() return now end, isConsoleMode = function() return arg[3] == "Console" end },
	engine = { config = { getModParams = function() return { lua_bridge = { evalAccess = arg[2] == "full" and 2 or 1 } } end } },
}
app, game = {}, {}

-- fake game script machinery: events are delivered to the engine hooks one frame after they are sent, and the shared
-- state is a plain table the GUI side reads through getComponent (the engine side gets it as `state`)
local store = {}
local fakeState = {
	get = function() return store.t end,
	set = function(_, t) store.t = t end,
	subscribeToEvent = function() end,
}
local sentEvents = {}
api.cmd = {
	makeScriptingSendEventCmd = function(file, id, name, param) return { id = id, name = name, param = param } end,
	sendCommand = function(cmd) sentEvents[#sentEvents + 1] = cmd end,
}
api.engine.system = { gameScriptSystem = { getEntityForGameScript = function() return 7 end } }
api.engine.getComponent = function() return { state = store.t or {} } end
api.type = { ComponentType = { GAME_SCRIPT = "GAME_SCRIPT" } }
local realRequire = require
require = function(name) if name == "io" then return ioLib end return realRequire(name) end
io = nil -- like the game: only reachable through require
local chunk = assert(loadfile(base .. "bridge.script.lua"))
chunk()
local script = data()
local started = os.time()
while os.time() - started < seconds do
	now = now + 0.004
	script.guiUpdate()
	script.update({}, fakeState, 0.1)
	local batch = sentEvents
	sentEvents = {}
	for _, ev in ipairs(batch) do script.handleEvent({}, fakeState, "", ev.id, ev.name, ev.param) end
	os.execute("sleep 0.004")
end
