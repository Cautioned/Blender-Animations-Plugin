-- Written by @boatbomber

local Plugin = script:FindFirstAncestorWhichIsA("Plugin")
local Fusion = require(Plugin:FindFirstChild("Fusion", true))

local StudioComponents = script.Parent
local StudioComponentsUtil = StudioComponents:FindFirstChild("Util")

local getState = require(StudioComponentsUtil.getState)
local themeProvider = require(StudioComponentsUtil.themeProvider)
local constants = require(StudioComponentsUtil.constants)
local unwrap = require(StudioComponentsUtil.unwrap)
local types = require(StudioComponentsUtil.types)
local stripProps = require(StudioComponentsUtil.stripProps)

local Computed = Fusion.Computed
local Hydrate = Fusion.Hydrate
local New = Fusion.New

local COMPONENT_ONLY_PROPERTIES = {
	"Enabled",
	"TextColorStyle",
	"TextColor3",
	"TextSize",
}

type LabelProperties = {
	Enabled: (boolean | types.StateObject<boolean>)?,
	[any]: any,
}

return function(props: LabelProperties): TextLabel
	local isEnabled = getState(props.Enabled, true)
	local textSize = props.TextSize or constants.TextSize * 1.3

	local newLabel = New "TextLabel" {
		Name = "Label",
		Position = UDim2.fromScale(0, 0),
		AnchorPoint = Vector2.new(0, 0),
		Text = "Label",
		Font = themeProvider:GetFont("Bold"),
		TextSize = textSize,
		BackgroundTransparency = 1,
		BorderSizePixel = 0,
		BorderMode = Enum.BorderMode.Inset,

		Size = Computed(function()
			return UDim2.new(1, 0, 0, unwrap(textSize))
		end),

		TextColor3 = props.TextColor3 or themeProvider:GetColor(props.TextColorStyle or Enum.StudioStyleGuideColor.MainText, Computed(function()
			if not unwrap(isEnabled) then
				return Enum.StudioStyleGuideModifier.Disabled
			end
			return Enum.StudioStyleGuideModifier.Default
		end)),
	}

	local hydrateProps = stripProps(props, COMPONENT_ONLY_PROPERTIES)
	return Hydrate(newLabel)(hydrateProps)
end
