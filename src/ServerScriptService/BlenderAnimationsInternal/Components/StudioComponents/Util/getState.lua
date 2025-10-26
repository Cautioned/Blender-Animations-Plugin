local Plugin = script:FindFirstAncestorWhichIsA("Plugin")
local Fusion = require(Plugin:FindFirstChild("Fusion", true))

local unwrap = require(script.Parent.unwrap)
local types = require(script.Parent.types)

type stateOrAny = types.StateObject<any> | any

return function(inputValue: stateOrAny, defaultValue: stateOrAny, mustBeKind: string?)
	local stateKind = mustBeKind or "Value"
	local isInputAState = unwrap(inputValue, false)~=inputValue
	local isDefaultAState = unwrap(defaultValue, false)~=defaultValue
	
	if isInputAState and (mustBeKind==nil or inputValue.kind==mustBeKind) then
		return inputValue
	elseif inputValue~=nil then
		return Fusion[stateKind](unwrap(inputValue))
	end
	
	return if isDefaultAState then defaultValue else Fusion[stateKind](defaultValue)
end