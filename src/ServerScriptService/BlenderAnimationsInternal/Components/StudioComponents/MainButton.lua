-- Roact version by @sircfenner
-- Ported to Fusion by @YasuYoshida

local Plugin = script:FindFirstAncestorWhichIsA("Plugin")
local Fusion = require(Plugin:FindFirstChild("Fusion", true))

local StudioComponents = script.Parent
local StudioComponentsUtil = StudioComponents:FindFirstChild("Util")

local Button = require(StudioComponents.Button)

local Children = Fusion.Children
local Hydrate = Fusion.Hydrate
local New = Fusion.New

local baseProperties = {
	TextColorStyle = Enum.StudioStyleGuideColor.DialogMainButtonText,
	BackgroundColorStyle = Enum.StudioStyleGuideColor.DialogMainButton,
	BorderColorStyle = Enum.StudioStyleGuideColor.ButtonBorder,
	Name = "MainButton",
}

return function(props: Button.ButtonProperties): TextButton
	for index,value in pairs(baseProperties) do
		if props[index]==nil then
			props[index] = value
		end
	end
	return Button(props)
end