-- Bridge game script. Its resource name (lua_bridge::/lua_bridge/bridge.gs) identifies it in saves:
-- never rename or move this file. It keeps no saved state.
function data()
	return {
		guiUpdateScript = { fileName = "bridge.script@guiUpdate" },
		-- engine-side experiment (see the EXPERIMENT section of bridge.script.lua)
		updateScript = { fileName = "bridge.script@update" },
		handleEventScript = { fileName = "bridge.script@handleEvent" },
	}
end
