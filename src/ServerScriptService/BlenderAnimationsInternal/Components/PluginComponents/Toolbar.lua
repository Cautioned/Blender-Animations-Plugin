local Fusion = require(script:FindFirstAncestor("BlenderAnimationsInternal").Packages.Fusion)

local Hydrate = Fusion.Hydrate

local COMPONENT_ONLY_PROPERTIES = {
	"Name",
	"Plugin",
}

type ToolbarProperties = {
	Name: string,
	Plugin: Plugin,
	[any]: any,
}

return function(props: ToolbarProperties): PluginToolbar
	local PluginInstance = props.Plugin
	assert(PluginInstance, "Toolbar requires a 'Plugin' property to be passed")
	
	local newToolbar = PluginInstance:CreateToolbar(props.Name)

	local hydrateProps = table.clone(props)
	for _,propertyName in pairs(COMPONENT_ONLY_PROPERTIES) do
		hydrateProps[propertyName] = nil
	end

	return Hydrate(newToolbar)(hydrateProps)
end
