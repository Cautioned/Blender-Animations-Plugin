-- Run in Studio's edit-mode command bar after generating and serving the cases.
-- See easing_playback_cases.py for the headless Blender command.
-- Uses fresh copies of repository modules; does not change the installed plugin.
local base = "http://127.0.0.1:18763"
local http = game:GetService("HttpService")
local previousHttp = http.HttpEnabled
local folder = Instance.new("Folder")
folder.Name = "__EasingPlaybackModules"
folder.Parent = game.ServerStorage
local ok, result = xpcall(function()
	http.HttpEnabled = true
	local function fetch(name)
		return http:JSONDecode(http:GetAsync(base .. "/" .. name .. ".json"))
	end
	for name, source in fetch("sources") do
		local module = Instance.new("ModuleScript")
		module.Name = name
		module.Source = source
		module.Parent = folder
	end
	local run = require(folder.Runner)
	local Rig = require(folder.Rig)
	local reports = {}
	local failures = 0
	for _, id in fetch("manifest") do
		local case = fetch(id)
		local report = run(Rig, assert(workspace:FindFirstChild(case.rig), "Missing template " .. case.rig), case)
		table.insert(reports, report)
		if not report.pass then failures += 1 end
	end
	return {cases = #reports, failures = failures, reports = reports}
end, debug.traceback)
http.HttpEnabled = previousHttp
folder:Destroy()
assert(ok, result)
print(http:JSONEncode(result))
assert(result.failures == 0, tostring(result.failures) .. " easing cases failed")
return result
