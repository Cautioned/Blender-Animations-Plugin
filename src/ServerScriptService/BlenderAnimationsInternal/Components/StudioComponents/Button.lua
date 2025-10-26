-- Roact version by @sircfenner
-- Ported to Fusion by @YasuYoshida

local Plugin = script:FindFirstAncestorWhichIsA("Plugin")
local Fusion = require(Plugin:FindFirstChild("Fusion", true))

local StudioComponents = script.Parent
local StudioComponentsUtil = StudioComponents:FindFirstChild("Util")

local BaseButton = require(StudioComponents.BaseButton)

local New = Fusion.New
local Children = Fusion.Children
local Hydrate = Fusion.Hydrate

export type ButtonProperties = BaseButton.BaseButtonProperties

return function(props: ButtonProperties): TextButton
	if not props.Name then
		props.Name = "Button"
	end

	local newButton = BaseButton(props)
	return newButton
end
