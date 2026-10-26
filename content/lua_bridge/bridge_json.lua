-- JSON for the bridge. Written for Lua 5.2 (no utf8 library, no integer division, no bitwise operators).
--   decode(text)       -> value | nil, errorMessage       (JSON null decodes to json.null)
--   encode(value,opts) -> text | nil, "too_large"          opts: maxDepth (6), maxItems (500), maxBytes (1048576)
--   raw(text)          -> a value the encoder copies in verbatim (text must already be valid JSON)
-- Game values that JSON cannot represent (functions, userdata, cycles, depth/size limits) become strings.

local M = {}

M.null = setmetatable({}, { __tostring = function() return "null" end })

-- decoded JSON objects carry this marker, so an empty one is encoded back as {} and not []
local OBJECT = {}

local RAW = {}
function M.raw(text) return setmetatable({ text = text }, RAW) end

---------------------------------------------------------------------------------------------------------------------
-- decoding

local ESC = { ['"'] = '"', ['\\'] = '\\', ['/'] = '/', b = '\b', f = '\f', n = '\n', r = '\r', t = '\t' }

local function utf8char(cp)
	if cp < 0x80 then return string.char(cp) end
	if cp < 0x800 then
		return string.char(0xC0 + math.floor(cp / 0x40), 0x80 + cp % 0x40)
	end
	if cp < 0x10000 then
		return string.char(0xE0 + math.floor(cp / 0x1000), 0x80 + math.floor(cp / 0x40) % 0x40, 0x80 + cp % 0x40)
	end
	return string.char(0xF0 + math.floor(cp / 0x40000), 0x80 + math.floor(cp / 0x1000) % 0x40,
		0x80 + math.floor(cp / 0x40) % 0x40, 0x80 + cp % 0x40)
end

local decodeValue

local function skip(s, i)
	return s:find("[^ \t\r\n]", i) or #s + 1
end

local function fail(i, msg) error({ pos = i, msg = msg }, 0) end

local function decodeString(s, i)
	local out, j = {}, i + 1
	while true do
		local a, b = s:find('["\\%c]', j)
		if not a then fail(i, "unterminated string") end
		out[#out + 1] = s:sub(j, a - 1)
		local c = s:sub(a, a)
		if c == '"' then return table.concat(out), a + 1 end
		if c ~= "\\" then fail(a, "control character in string") end
		local e = s:sub(a + 1, a + 1)
		if e == "u" then
			local hex = s:match("^%x%x%x%x", a + 2)
			if not hex then fail(a, "bad \\u escape") end
			local cp = tonumber(hex, 16)
			j = a + 6
			if cp >= 0xD800 and cp < 0xDC00 then -- surrogate pair
				local lo = s:match("^\\u(%x%x%x%x)", j)
				local lcp = lo and tonumber(lo, 16)
				if lcp and lcp >= 0xDC00 and lcp < 0xE000 then
					cp = 0x10000 + (cp - 0xD800) * 0x400 + (lcp - 0xDC00)
					j = j + 6
				else
					cp = 0xFFFD
				end
			elseif cp >= 0xDC00 and cp < 0xE000 then
				cp = 0xFFFD
			end
			out[#out + 1] = utf8char(cp)
		else
			if not ESC[e] then fail(a, "bad escape") end
			out[#out + 1] = ESC[e]
			j = a + 2
		end
	end
end

function decodeValue(s, i, depth)
	if depth > 64 then fail(i, "nesting too deep") end
	i = skip(s, i)
	local c = s:sub(i, i)
	if c == "{" then
		local t = setmetatable({}, OBJECT)
		i = skip(s, i + 1)
		if s:sub(i, i) == "}" then return t, i + 1 end
		while true do
			if s:sub(i, i) ~= '"' then fail(i, "expected string key") end
			local k
			k, i = decodeString(s, i)
			i = skip(s, i)
			if s:sub(i, i) ~= ":" then fail(i, "expected ':'") end
			t[k], i = decodeValue(s, i + 1, depth + 1)
			i = skip(s, i)
			local d = s:sub(i, i)
			if d == "}" then return t, i + 1 end
			if d ~= "," then fail(i, "expected ',' or '}'") end
			i = skip(s, i + 1)
		end
	elseif c == "[" then
		local t, n = {}, 0
		i = skip(s, i + 1)
		if s:sub(i, i) == "]" then return t, i + 1 end
		while true do
			n = n + 1
			t[n], i = decodeValue(s, i, depth + 1)
			i = skip(s, i)
			local d = s:sub(i, i)
			if d == "]" then return t, i + 1 end
			if d ~= "," then fail(i, "expected ',' or ']'") end
			i = i + 1
		end
	elseif c == '"' then
		return decodeString(s, i)
	elseif s:sub(i, i + 3) == "true" then return true, i + 4
	elseif s:sub(i, i + 4) == "false" then return false, i + 5
	elseif s:sub(i, i + 3) == "null" then return M.null, i + 4
	end
	local num = s:match("^-?%d+%.?%d*[eE]?[+-]?%d*", i)
	if num and num ~= "" and tonumber(num) then return tonumber(num), i + #num end
	fail(i, "unexpected character")
end

function M.decode(text)
	local ok, v, i = pcall(decodeValue, text, 1, 0)
	if not ok then
		if type(v) == "table" then return nil, string.format("%s at %d", v.msg, v.pos) end
		return nil, tostring(v)
	end
	if skip(text, i) <= #text then return nil, "trailing characters at " .. skip(text, i) end
	return v
end

---------------------------------------------------------------------------------------------------------------------
-- encoding

local ESCAPES = { ['"'] = '\\"', ['\\'] = '\\\\', ['\b'] = '\\b', ['\f'] = '\\f', ['\n'] = '\\n', ['\r'] = '\\r', ['\t'] = '\\t' }

-- replaces bytes that are not valid UTF-8 with U+FFFD so the output is always valid JSON text
local function cleanUtf8(s)
	if not s:find("[\128-\255]") then return s end
	local out, i, n = {}, 1, #s
	while i <= n do
		local b = s:byte(i)
		local len = (b < 0x80 and 1) or (b >= 0xC2 and b < 0xE0 and 2) or (b >= 0xE0 and b < 0xF0 and 3)
			or (b >= 0xF0 and b < 0xF5 and 4) or 0
		local ok = len > 0 and i + len - 1 <= n
		if ok then
			for k = i + 1, i + len - 1 do
				local cb = s:byte(k)
				if cb < 0x80 or cb > 0xBF then ok = false; break end
			end
		end
		if ok then
			out[#out + 1] = s:sub(i, i + len - 1)
			i = i + len
		else
			out[#out + 1] = "\239\191\189"
			i = i + 1
		end
	end
	return table.concat(out)
end

local function quote(s)
	return '"' .. cleanUtf8(s):gsub('[%c"\\]', function(c)
		return ESCAPES[c] or string.format("\\u%04x", c:byte())
	end) .. '"'
end

local function number(n)
	if n ~= n or n == math.huge or n == -math.huge then return "null" end
	if n == math.floor(n) and math.abs(n) < 2 ^ 53 then return string.format("%d", n) end
	return string.format("%.14g", n)
end

local function describe(v)
	local ok, s = pcall(tostring, v)
	return ok and s or ("<" .. type(v) .. ">")
end

function M.encode(value, opts)
	opts = opts or {}
	local maxDepth, maxItems, maxBytes = opts.maxDepth or 6, opts.maxItems or 500, opts.maxBytes or 1048576
	local out, size, seen = {}, 0, {}
	local overflow = {}

	local function put(s)
		size = size + #s
		if size > maxBytes then error(overflow, 0) end
		out[#out + 1] = s
	end

	local enc
	local function encTable(t, depth)
		local mt = getmetatable(t)
		if mt == RAW then put(t.text); return end
		if t == M.null then put("null"); return end
		if seen[t] then put(quote("<cycle>")); return end
		if depth > maxDepth then put(quote(describe(t) .. " (depth limit)")); return end
		seen[t] = true
		local n, arrayLike, count = 0, true, 0
		for k in pairs(t) do
			count = count + 1
			if type(k) == "number" and k >= 1 and k == math.floor(k) then
				if k > n then n = k end
			else
				arrayLike = false
			end
		end
		if mt == OBJECT and count == 0 then
			put("{}")
		elseif arrayLike and n == count then
			put("[")
			for i = 1, math.min(n, maxItems) do
				if i > 1 then put(",") end
				enc(t[i], depth + 1)
			end
			if n > maxItems then put(",") put(quote("... " .. (n - maxItems) .. " more items")) end
			put("]")
		else
			local keys = {}
			for k in pairs(t) do keys[#keys + 1] = k end
			table.sort(keys, function(a, b)
				local ta, tb = type(a), type(b)
				if ta == tb and (ta == "string" or ta == "number") then return a < b end
				return tostring(a) < tostring(b)
			end)
			put("{")
			for i = 1, math.min(#keys, maxItems) do
				if i > 1 then put(",") end
				put(quote(type(keys[i]) == "string" and keys[i] or describe(keys[i])))
				put(":")
				enc(t[keys[i]], depth + 1)
			end
			if #keys > maxItems then put(",") put(quote("__truncated__")) put(":") put(quote((#keys - maxItems) .. " more keys")) end
			put("}")
		end
		seen[t] = nil
	end

	function enc(v, depth)
		local ty = type(v)
		if v == nil then put("null")
		elseif ty == "boolean" then put(v and "true" or "false")
		elseif ty == "number" then put(number(v))
		elseif ty == "string" then put(quote(v))
		elseif ty == "table" then encTable(v, depth)
		else put(quote(describe(v))) end -- function, userdata, thread
	end

	local ok, err = pcall(enc, value, 0)
	if not ok then
		if err == overflow then return nil, "too_large" end
		error(err, 0)
	end
	return table.concat(out)
end

return M
