-- Bridge: lets an external client (the MCP server) run Lua in the game's GUI state and read the results.
-- Protocol and rationale: .okf/mods/lua-bridge.md in the workspace. Summary:
--   <TEMP>\tf3_bridge\in.jsonl    one JSON request per line, appended by the client
--   <TEMP>\tf3_bridge\out.jsonl   one JSON response per line, appended here
-- The GUI state is one persistent Lua state, so the open file handles and the line buffer live here. The files
-- are only ever appended to; this script tails in.jsonl through a held handle, and never truncates anything.
-- Lua here is 5.2; file access goes through the io module.

local MOD = (getOwningModId and getOwningModId()) or "lua_bridge"
local json = ug_require(MOD .. "::/lua_bridge/bridge_json.lua")
local hasIo, io = pcall(require, "io")

local VERSION = 1
local DIR_NAME = "tf3_bridge"
local IN_NAME, OUT_NAME = "in.jsonl", "out.jsonl"
local PROBE_SEC = 0.5 -- while there is no client: how often to look for in.jsonl
local SIZE_CHECK_SEC = 1 -- how often to check whether the input file was truncated under us
local FRAME_BUDGET = 0.004 -- CPU seconds of requests per frame (a single request may exceed it)
local MAX_LINES_PER_FRAME = 200
local MAX_QUEUE = 1000
local MAX_LINE = 8 * 1024 * 1024 -- longest request line accepted
local MAX_RESULT = 1024 * 1024 -- longest encoded eval result
local MAX_ENGINE_RESULT = 256 * 1024 -- the engine hands results to the GUI through the game script state, which is saved with the game
local ENGINE_TIMEOUT = 10 -- seconds to wait for an engine answer
-- GUI <-> engine channel (no files): requests travel as script events, answers through the game script state.
-- Subscriptions are by event NAME; the id is only a label (the base game sends ("", "Towns", "OnLevelUp", ...)).
local EVENT_ID = "LuaBridge"
local REQUEST_NAME, ACK_NAME = "LuaBridgeRequest", "LuaBridgeAck"
local GS_NAME = MOD .. "::/lua_bridge/bridge.gs"
local MAX_LOG_LINES, MAX_LOG_CHARS = 100, 1000

local function say(msg)
	local text = "[lua_bridge] " .. msg
	if debugPrint then pcall(debugPrint, text) else print(text) end
end

-- state survives in package.loaded so that nothing depends on how often the game evaluates this file
local G = package.loaded.lua_bridge_state
if G == nil then
	G = { failures = 0, nextOpen = 0, nextSizeCheck = 0, queue = {}, qhead = 1, qtail = 0, buf = "", logged = 0 }
	package.loaded.lua_bridge_state = G
end
G.pending = G.pending or {} -- engine requests waiting for an answer: seq -> { context, t0 }
G.engineSeq = G.engineSeq or 0

local function clock()
	local ok, t = pcall(api.util.getApplicationTime)
	if ok and type(t) == "number" then return t end
	return os.clock()
end

---------------------------------------------------------------------------------------------------------------------
-- mod settings (mod.json params): a 1-based index into these lists

local OPTION_VALUES = { evalAccess = { "restricted", "full" } }
local options
local function opt(key)
	if options == nil then
		options = {}
		local raw = {}
		pcall(function() raw = api.engine.config.getModParams()[MOD] or {} end)
		for k, values in pairs(OPTION_VALUES) do
			local idx
			pcall(function() idx = raw[k] end)
			options[k] = values[idx or 1] or values[1]
		end
	end
	return options[key]
end

---------------------------------------------------------------------------------------------------------------------
-- the environment that eval code runs in. It persists, so `x = 1` in one eval is visible in the next.

local RESTRICTED_GLOBALS = {
	"api", "app", "game", "assert", "error", "ipairs", "next", "pairs", "pcall", "xpcall", "select", "tonumber",
	"tostring", "type", "rawequal", "rawget", "rawlen", "rawset", "getmetatable", "setmetatable", "math", "string",
	"table", "bit32", "coroutine", "debugPrint", "toString", "toStringInline", "serializeStr", "simpleSerializeStr",
	"orderedPairs", "getTableSize", "getBuildVersion", "getBuildOS", "_VERSION",
}

local function describe(v)
	local ok, s = pcall(tostring, v)
	return ok and s or ("<" .. type(v) .. ">")
end

local function getEnv()
	if G.env then return G.env end
	local env
	if opt("evalAccess") == "full" then
		env = setmetatable({}, { __index = _G })
	else
		-- best effort: no require, package, debug, load or io. Whatever the game objects themselves expose stays.
		env = {}
		for _, name in ipairs(RESTRICTED_GLOBALS) do env[name] = _G[name] end
		env.os = { time = os.time, clock = os.clock, date = os.date, difftime = os.difftime }
		env._G = env
	end
	env.print = function(...)
		local parts = {}
		for i = 1, select("#", ...) do parts[i] = describe((select(i, ...))) end
		local log = G.captured
		if log and #log < MAX_LOG_LINES then log[#log + 1] = table.concat(parts, "\t"):sub(1, MAX_LOG_CHARS) end
	end
	G.env = env
	return env
end

---------------------------------------------------------------------------------------------------------------------
-- files

-- `context` is whatever JSON value the request carried in its own `context` field; it is copied through untouched
local function respond(obj, context)
	if context ~= nil then obj.context = context end
	local text, err = json.encode(obj, { maxDepth = 8, maxItems = 1000, maxBytes = MAX_RESULT + 65536 })
	if not text then
		text = json.encode({ ok = false, error = "response_too_large (" .. tostring(err) .. ")", context = context })
	end
	local ok, werr = pcall(function()
		G.outF:write(text, "\n")
		G.outF:flush()
	end)
	if not ok then error("writing out.jsonl failed: " .. tostring(werr), 0) end
end

local function disconnect(reason)
	if G.inF then pcall(function() G.inF:close() end) end
	if G.outF then pcall(function() G.outF:close() end) end
	G.inF, G.outF, G.buf, G.queue, G.qhead, G.qtail, G.discard = nil, nil, "", {}, 1, 0, false
	G.pending, G.gsEntity = {}, nil
	if reason then say("disconnected: " .. reason) end
end

-- The bridge talks through files in %TEMP%, so it only runs on Windows (the path separator tells); anywhere else, or
-- where io is unavailable, it stays idle. Not api.util.string2Platform: an unknown name is a fatal engine assertion.
local function supported()
	if G.supported == nil then
		G.supported = hasIo and package.config:sub(1, 1) == "\\"
		if not G.supported then say("Lua Bridge only works on Windows; staying idle") end
	end
	return G.supported
end

local function connect()
	if not supported() then return false end
	local now = clock()
	if now < G.nextOpen then return false end
	G.nextOpen = now + PROBE_SEC
	local tmp = os.getenv("TEMP") or os.getenv("TMP")
	if not tmp then return false end
	local sep = package.config:sub(1, 1)
	local dir = tmp .. sep .. DIR_NAME .. sep
	local inF = io.open(dir .. IN_NAME, "rb")
	if not inF then return false end
	local outF = io.open(dir .. OUT_NAME, "ab")
	if not outF then inF:close(); return false end
	inF:seek("end") -- history belongs to earlier runs; clients (re)send hello until they get an answer
	G.inF, G.outF = inF, outF
	G.buf, G.queue, G.qhead, G.qtail, G.discard = "", {}, 1, 0, false
	G.pending, G.gsEntity = {}, nil
	G.run = string.format("%x-%x", os.time(), math.floor(now * 1000) % 1000000)
	G.nextSizeCheck = now + SIZE_CHECK_SEC
	say("watching " .. dir .. IN_NAME .. " (run " .. G.run .. ")")
	return true
end

-- takes every complete line from the input file; whatever follows the last newline stays in G.buf
local function readLines()
	local f = G.inF
	local now = clock()
	if now >= G.nextSizeCheck then
		G.nextSizeCheck = now + SIZE_CHECK_SEC
		local cur = f:seek("cur")
		local size = f:seek("end")
		f:seek("set", cur)
		if size < cur then
			f:seek("set", 0)
			G.buf, G.discard = "", false
			say("in.jsonl was truncated; reading from its start")
		end
	end
	for _ = 1, MAX_LINES_PER_FRAME do
		local l = f:read("*L")
		if not l then return end
		if l:sub(-1) ~= "\n" then
			if not G.discard then
				G.buf = G.buf .. l
				if #G.buf > MAX_LINE then
					G.buf, G.discard = "", true
					respond({ ok = false, error = "request_too_large: lines may be at most " .. MAX_LINE .. " bytes" })
				end
			end
			return
		end
		local line = G.buf .. l
		G.buf = ""
		if G.discard then
			G.discard = false -- this was the end of an oversize line
		else
			line = line:sub(1, -2):gsub("\r$", "")
			if line:find("%S") then
				if G.qtail - G.qhead + 1 >= MAX_QUEUE then
					local req = json.decode(line)
					respond({ ok = false, error = "queue_full" }, type(req) == "table" and req.context or nil)
				else
					G.qtail = G.qtail + 1
					G.queue[G.qtail] = line
				end
			end
		end
	end
end

---------------------------------------------------------------------------------------------------------------------
-- requests

local function pack(...) return { n = select("#", ...), ... } end

local function handleTraceback(e)
	return { msg = describe(e), tb = (debug and debug.traceback) and debug.traceback("", 2) or "" }
end

-- Compiles and runs `code` in the persistent environment. Returns a plain table (strings and numbers only, so it can
-- also travel through the game script state): { ok, resultJson | kind, error, traceback, log, ms, truncated }.
local function evaluate(code, limits)
	local env = getEnv()
	local fn, cerr = load("return " .. code, "=eval", "t", env) -- an expression first, then a statement list
	if not fn then fn, cerr = load(code, "=eval", "t", env) end
	if not fn then return { ok = false, kind = "syntax", error = tostring(cerr) } end
	G.captured = {}
	local t0 = os.clock()
	local res = pack(xpcall(fn, handleTraceback))
	local ms = math.floor((os.clock() - t0) * 10000 + 0.5) / 10
	local log = #G.captured > 0 and G.captured or nil
	G.captured = nil
	if not res[1] then
		local e = res[2]
		return { ok = false, kind = "runtime", error = e.msg, traceback = e.tb:sub(1, 4096), log = log, ms = ms }
	end
	local value
	if res.n == 2 then
		value = res[2]
	elseif res.n > 2 then
		-- not table.unpack: the game replaces it with a version that ignores its range arguments
		value = {}
		for i = 2, res.n do
			if res[i] == nil then value[i - 1] = json.null else value[i - 1] = res[i] end
		end
	end
	local text = json.encode(value, { maxDepth = limits.maxDepth, maxItems = limits.maxItems, maxBytes = limits.maxBytes })
	local truncated
	if not text then
		truncated = true
		text = json.encode(value, { maxDepth = 2, maxItems = 50, maxBytes = limits.maxBytes })
			or json.encode(describe(value):sub(1, 2000))
	end
	return { ok = true, resultJson = text, truncated = truncated, log = log, ms = ms }
end

local function toResponse(r)
	if r.ok then
		return { ok = true, result = json.raw(r.resultJson), truncated = r.truncated, log = r.log, ms = r.ms,
			engineState = r.engineState }
	end
	return { ok = false, kind = r.kind, error = r.error, traceback = r.traceback, log = r.log, ms = r.ms,
		engineState = r.engineState }
end

local function limitsOf(req, maxBytes)
	return { maxDepth = tonumber(req.maxDepth) or 6, maxItems = tonumber(req.maxItems) or 500, maxBytes = maxBytes }
end

local function sendEvent(name, param)
	api.cmd.sendCommand(api.cmd.makeScriptingSendEventCmd("", EVENT_ID, name, param))
end

-- the game script's state, written by the engine hooks and readable here
local function sharedState()
	if not G.gsEntity then
		G.gsEntity = api.engine.system.gameScriptSystem.getEntityForGameScript(GS_NAME)
	end
	local comp = api.engine.getComponent(G.gsEntity, api.type.ComponentType.GAME_SCRIPT)
	return comp and comp.state
end

local function startEngineEval(req)
	G.engineSeq = G.engineSeq + 1
	local seq = G.run .. "-" .. G.engineSeq
	local limits = limitsOf(req, MAX_ENGINE_RESULT)
	local ok, err = pcall(sendEvent, REQUEST_NAME, { seq = seq, code = req.code, maxDepth = limits.maxDepth,
		maxItems = limits.maxItems })
	if not ok then
		return respond({ ok = false, error = "engine_send_failed: " .. tostring(err) }, req.context)
	end
	G.pending[seq] = { context = req.context, t0 = clock() }
end

local function pollEngineInner()
	local st = sharedState()
	local responses = st and st.responses
	local now = clock()
	for seq, p in pairs(G.pending) do
		local r = responses and responses[seq]
		if r then
			G.pending[seq] = nil
			respond(toResponse(r), p.context)
			pcall(sendEvent, ACK_NAME, { seq = seq }) -- lets the engine drop the stored answer
		elseif now - p.t0 > ENGINE_TIMEOUT then
			G.pending[seq] = nil
			respond({ ok = false, error = "engine_timeout: no answer within " .. ENGINE_TIMEOUT .. " s" }, p.context)
			pcall(sendEvent, ACK_NAME, { seq = seq })
		end
	end
end

local function pollEngine()
	if next(G.pending) == nil then return end
	local ok, err = pcall(pollEngineInner)
	if not ok then
		-- an unreadable shared state fails every pending request instead of looping on the error
		for seq, p in pairs(G.pending) do
			G.pending[seq] = nil
			respond({ ok = false, error = "engine_unavailable: " .. tostring(err) }, p.context)
		end
	end
end

local function runEval(req)
	local code = req.code
	if type(code) ~= "string" then
		return respond({ ok = false, error = "eval needs a string field 'code'" }, req.context)
	end
	if req.state == "engine" then
		return startEngineEval(req)
	elseif req.state ~= nil and req.state ~= "gui" then
		return respond({ ok = false, error = "unsupported_state: use 'gui' (default) or 'engine'" }, req.context)
	end
	respond(toResponse(evaluate(code, limitsOf(req, MAX_RESULT))), req.context)
end

local function handle(line)
	local req, err = json.decode(line)
	if type(req) ~= "table" or req == json.null or req[1] ~= nil then
		return respond({ ok = false, error = "bad_request: " .. tostring(err or "a request must be a JSON object") })
	end
	local op = req.op
	if op == "eval" then
		runEval(req)
	elseif op == "hello" then
		local build
		pcall(function() build = getBuildVersion() end)
		respond({ op = "hello", ok = true, version = VERSION, run = G.run, state = "gui", states = { "gui", "engine" },
			access = opt("evalAccess"), build = build }, req.context)
	elseif op == "ping" then
		respond({ op = "ping", ok = true, time = clock() }, req.context)
	else
		respond({ ok = false, error = "unknown_op: " .. describe(op) }, req.context)
	end
end

local function runQueue(t0)
	while G.qhead <= G.qtail do
		local line = G.queue[G.qhead]
		G.queue[G.qhead] = nil
		G.qhead = G.qhead + 1
		handle(line)
		if os.clock() - t0 > FRAME_BUDGET then break end
	end
	if G.qhead > G.qtail then G.queue, G.qhead, G.qtail = {}, 1, 0 end
end

---------------------------------------------------------------------------------------------------------------------

local function step()
	if G.pausedUntil then
		if clock() < G.pausedUntil then return end
		G.pausedUntil = nil
	end
	if not G.inF and not connect() then return end
	local t0 = os.clock()
	readLines()
	runQueue(t0)
	pollEngine()
end

local script = {}

function script.guiUpdate()
	local ok, err = pcall(step)
	if ok then
		G.failures = 0
		return
	end
	G.failures = G.failures + 1
	if G.logged < 20 then
		G.logged = G.logged + 1
		say("error (" .. G.failures .. "): " .. tostring(err))
	end
	if G.failures >= 5 then
		disconnect("too many errors; retrying in 30 s")
		G.failures = 0
		G.pausedUntil = clock() + 30
	end
end

---------------------------------------------------------------------------------------------------------------------
-- Engine side. The engine runs these hooks in a pool of Lua states, so nothing here keeps per-request state in Lua:
-- a request arrives as an event (only delivered to hooks that subscribed to its NAME), is evaluated in the state
-- that handles the event, and the answer is stored in the game script state, which the GUI polls (see
-- pollEngine) and clears with an ack event. Everything is wrapped in pcall: an error in a hook is logged by the
-- game on every call.

local function subscribe(state)
	if not G.subscribed then
		G.subscribed = true
		state:subscribeToEvent(REQUEST_NAME)
		state:subscribeToEvent(ACK_NAME)
	end
end

function script.update(userParams, state, dt)
	pcall(subscribe, state)
end

function script.handleEvent(userParams, state, src, id, name, param)
	pcall(function()
		subscribe(state)
		if (name ~= REQUEST_NAME and name ~= ACK_NAME) or type(param) ~= "table" or param.seq == nil then return end
		local key = tostring(param.seq)
		local t = state:get() or {}
		if name == ACK_NAME then
			if t.responses then
				t.responses[key] = nil
				state:set(t)
			end
			return
		end
		local ok, r = pcall(evaluate, tostring(param.code), { maxDepth = tonumber(param.maxDepth) or 6,
			maxItems = tonumber(param.maxItems) or 500, maxBytes = MAX_ENGINE_RESULT })
		if not ok then r = { ok = false, kind = "internal", error = tostring(r) } end
		G.tag = G.tag or (tostring({}):gsub("table: ", ""))
		r.engineState = G.tag
		t = state:get() or {}
		t.responses = t.responses or {}
		t.responses[key] = r
		state:set(t)
	end)
end

-- .script.lua resources expose their exports through data()
function data()
	return script
end
