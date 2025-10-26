local Plugin = script:FindFirstAncestorWhichIsA("Plugin")
local Fusion = require(Plugin:FindFirstChild("Fusion", true))

local StudioComponents = script.Parent
local StudioComponentsUtil = StudioComponents:FindFirstChild("Util")

local themeProvider = require(StudioComponentsUtil.themeProvider)
local stripProps = require(StudioComponentsUtil.stripProps)
local types = require(StudioComponentsUtil.types)

local New = Fusion.New
local Hydrate = Fusion.Hydrate

local COMPONENT_ONLY_PROPERTIES = {
	"StudioStyleGuideColor",
	"StudioStyleGuideModifier"
}

type BackgroundProperties = {
	StudioStyleGuideColor: types.CanBeState<Enum.StudioStyleGuideColor>?,
	StudioStyleGuideModifier: types.CanBeState<Enum.StudioStyleGuideModifier>?,
	[any]: any,
}

return function(props: BackgroundProperties): Frame
	return Hydrate(New "Frame" {
		Size = UDim2.fromScale(1, 1),
		Position = UDim2.fromScale(0, 0),
		AnchorPoint = Vector2.new(0, 0),
		LayoutOrder = 0,
		ZIndex = 1,
		BorderSizePixel = 0,
		BackgroundColor3 = themeProvider:GetColor(
			props.StudioStyleGuideColor or Enum.StudioStyleGuideColor.MainBackground, 
			props.StudioStyleGuideModifier
		),
	})(stripProps(props, COMPONENT_ONLY_PROPERTIES))
end