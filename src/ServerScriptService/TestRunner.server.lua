local testez = require(script.Parent.BlenderAnimationsInternal.Components.testez)

-- The directory containing the tests.
local test_directory = script.Parent.BlenderAnimationsInternal.tests

-- As per the documentation, run expects a table of test roots.
local success, result_or_error = pcall(function()
	return testez.TestBootstrap:run({ test_directory })
end)

if not success then
	error("testez failed to run: " .. tostring(result_or_error))
	return
end

local results = result_or_error
if (results.failure_count or 0) > 0 or (results.error_count or 0) > 0 then
	error("tests failed!")
else
	print("all tests passed!")
end 