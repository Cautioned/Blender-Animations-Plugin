local Plugin = script:FindFirstAncestorWhichIsA("Plugin")
local Fusion = require(Plugin:FindFirstChild("Fusion", true))

local Hydrate = Fusion.Hydrate

local COMPONENT_ONLY_PROPERTIES = {
	"Name",
}

type ToolbarProperties = {
	Name: string,
	[any]: any,
}

return function(props: ToolbarProperties): PluginToolbar
	local newToolbar = Plugin:CreateToolbar(props.Name)

	local hydrateProps = table.clone(props)
	for _,propertyName in pairs(COMPONENT_ONLY_PROPERTIES) do
		hydrateProps[propertyName] = nil
	end

	return Hydrate(newToolbar)(hydrateProps)
end
