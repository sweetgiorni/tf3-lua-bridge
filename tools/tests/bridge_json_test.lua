-- Offline test for content/lua_bridge/bridge_json.lua. Run: mise exec -- lua tools/tests/bridge_json_test.lua
package.path = "content/lua_bridge/?.lua;" .. package.path
local json = require("bridge_json")
local fails = 0
local function check(name, cond, extra) if not cond then fails = fails + 1; print("FAIL " .. name .. (extra and (": " .. tostring(extra)) or "")) end end

local v = json.decode('{"id":7,"op":"eval","code":"return \\"a\\\\nb\\" .. \\u00e9 \\ud83d\\ude00","n":[1,2.5,-3e2,true,false,null],"o":{}}')
check("decode object", v and v.id == 7 and v.op == "eval")
check("decode unicode", v.code == 'return "a\\nb" .. \195\169 \240\159\152\128', v.code)
check("decode array", v.n[1] == 1 and v.n[2] == 2.5 and v.n[3] == -300 and v.n[4] == true and v.n[5] == false and v.n[6] == json.null)
check("decode empty obj", next(v.o) == nil)
for _, bad in ipairs{ "", "{", '{"a":}', '[1,]', '{"a":1} x', '"abc', '{"a" 1}', "nul", '"\\x"' } do
	local r, err = json.decode(bad)
	check("reject " .. bad, r == nil and err, err)
end
check("deep nesting rejected", json.decode(string.rep("[", 100) .. string.rep("]", 100)) == nil)

check("enc scalars", json.encode({ 1, "a", true }) == '[1,"a",true]')
check("enc object sorted", json.encode({ b = 1, a = { 2 } }) == '{"a":[2],"b":1}')
check("enc empty is array", json.encode({}) == "[]")
check("enc nil/nan/inf", json.encode(nil) == "null" and json.encode(0/0) == "null" and json.encode(math.huge) == "null")
check("enc integer-valued", json.encode(3) == "3" and json.encode(3.5) == "3.5" and json.encode(2^40) == "1099511627776")
check("enc escapes", json.encode('q"\\\n\1') == '"q\\"\\\\\\n\\u0001"', json.encode('q"\\\n\1'))
check("enc bad utf8", json.encode("a\255b\194") == '"a\239\191\189b\239\191\189"')
check("enc good utf8", json.encode("\195\169") == '"\195\169"')
check("enc function", json.encode(print):find("^\"builtin") or json.encode(print):find("^\"function"))
local cyc = {} cyc.self = cyc
check("enc cycle", json.encode(cyc) == '{"self":"<cycle>"}', json.encode(cyc))
local deep = { { { { { { { { 1 } } } } } } } }
check("enc depth", json.encode(deep, { maxDepth = 2 }):find("depth limit"))
local big = {} for i = 1, 600 do big[i] = i end
check("enc items", json.encode(big):find("100 more items"))
check("enc too large", select(2, json.encode({ string.rep("x", 100) }, { maxBytes = 50 })) == "too_large")
check("enc raw", json.encode({ r = json.raw('{"x":1}') }) == '{"r":{"x":1}}')
check("empty object survives decode+encode", json.encode(json.decode("{}")) == "{}" and json.encode(json.decode("[]")) == "[]" and json.encode(json.decode('{"a":{},"b":[]}')) == '{"a":{},"b":[]}', json.encode(json.decode('{"a":{},"b":[]}')))
local round = json.decode(json.encode({ a = { 1, 2, { b = "c\n" } }, s = "é" }))
check("roundtrip", round.a[3].b == "c\n" and round.s == "é")
-- Lua 5.2 compatibility: nothing newer than 5.2 may be used
local src = io.open("content/lua_bridge/bridge_json.lua"):read("*a")
for _, pat in ipairs{ "utf8%.", "string%.pack", "table%.move", "math%.type", "//", "<<", ">>", "~=%s*~" } do
	check("no 5.3-ism " .. pat, not src:gsub("%-%-[^\n]*", ""):find(pat))
end
print(fails == 0 and "bridge_json: all tests passed" or ("bridge_json: " .. fails .. " FAILED"))
os.exit(fails == 0 and 0 or 1)
