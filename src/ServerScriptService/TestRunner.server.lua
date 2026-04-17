local testez = require(script.Parent.BlenderAnimationsInternal.Components.testez)

local TEST_NODE_TYPE_IT = "It"
local TEST_STATUS_FAILURE = "Failure"

local DEFAULT_TEST_ROOTS = {
	script.Parent.BlenderAnimationsInternal.tests,
}

local function getBooleanAttribute(attributeName: string, defaultValue: boolean): boolean
	local value = script:GetAttribute(attributeName)
	if value == nil then
		return defaultValue
	end
	return value == true
end

local function getStringAttribute(attributeName: string): string?
	local value = script:GetAttribute(attributeName)
	if type(value) == "string" and value ~= "" then
		return value
	end
	return nil
end

local function collectFailingTests(results)
	local failingTests = {}

	local function visit(node, path)
		local phrase = node.planNode and node.planNode.phrase
		local nextPath = path
		if phrase and phrase ~= "" then
			nextPath = table.clone(path)
			table.insert(nextPath, phrase)
		end

		if node.planNode and node.planNode.type == TEST_NODE_TYPE_IT and node.status == TEST_STATUS_FAILURE then
			table.insert(failingTests, {
				path = table.concat(nextPath, " > "),
				errors = node.errors,
			})
		end

		for _, child in ipairs(node.children) do
			visit(child, nextPath)
		end
	end

	for _, child in ipairs(results.children) do
		visit(child, {})
	end

	return failingTests
end

local testNamePattern = getStringAttribute("TestNamePattern")
local showTimingInfo = getBooleanAttribute("ShowTimingInfo", true)
local quietReporter = getBooleanAttribute("QuietReporter", true)
local reporter = if quietReporter then testez.Reporters.TextReporterQuiet else testez.Reporters.TextReporter

print("[testrunner] starting tests")
if testNamePattern then
	print("[testrunner] filter: " .. testNamePattern)
end

local success, resultOrError = pcall(function()
	return testez.TestBootstrap:run(DEFAULT_TEST_ROOTS, reporter, {
		showTimingInfo = showTimingInfo,
		testNamePattern = testNamePattern,
	})
end)

if not success then
	error("testez failed to run: " .. tostring(resultOrError))
end

local results = resultOrError
local summary = string.format(
	"[testrunner] completed: %d passed, %d failed, %d skipped, %d errors",
	results.successCount or 0,
	results.failureCount or 0,
	results.skippedCount or 0,
	#(results.errors or {})
)
print(summary)

if (results.failureCount or 0) > 0 then
	local failingTests = collectFailingTests(results)
	print("[testrunner] failing tests:")
	for _, failingTest in ipairs(failingTests) do
		print(" - " .. failingTest.path)
		for _, message in ipairs(failingTest.errors) do
			print("   " .. tostring(message))
		end
	end
	if #failingTests == 0 then
		print(" - failures were reported but no leaf test nodes were collected")
	end
end

if #(results.errors or {}) > 0 then
	print("[testrunner] reported errors:")
	for _, message in ipairs(results.errors) do
		print(" - " .. tostring(message))
	end
end

if (results.failureCount or 0) > 0 or #(results.errors or {}) > 0 then
	error(summary)
end

print("[testrunner] all tests passed")