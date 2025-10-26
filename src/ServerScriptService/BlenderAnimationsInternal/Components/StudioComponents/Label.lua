-- Roact version by @sircfenner
-- Ported to Fusion by @YasuYoshida

local Plugin = script:FindFirstAncestorWhichIsA("Plugin")
local Fusion = require(Plugin:FindFirstChild("Fusion", true))

local StudioComponents = script.Parent
local StudioComponentsUtil = StudioComponents:FindFirstChild("Util")

local getMotionState = require(StudioComponentsUtil.getMotionState)
local themeProvider = require(StudioComponentsUtil.themeProvider)
local getModifier = require(StudioComponentsUtil.getModifier)
local stripProps = require(StudioComponentsUtil.stripProps)
local constants = require(StudioComponentsUtil.constants)
local getState = require(StudioComponentsUtil.getState)
local unwrap = require(StudioComponentsUtil.unwrap)
local types = require(StudioComponentsUtil.types)

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
	local textSize = props.TextSize or constants.TextSize

	local mainModifier = getModifier({
		Enabled = isEnabled
	})

	local textContent = props.Text or "Label"

	local newLabel = New "TextLabel" {
		Name = "Label",
		Position = UDim2.fromScale(0, 0),
		AnchorPoint = Vector2.new(0, 0),
		Text = textContent,
		TextSize = textSize,
		BackgroundTransparency = 1,
		BorderSizePixel = 0,
		BorderMode = Enum.BorderMode.Inset,
		TextWrapped = true, -- Enable text wrapping
		Font = themeProvider:GetFont("Default"),
		TextColor3 = props.TextColor3 or getMotionState(themeProvider:GetColor(props.TextColorStyle or Enum.StudioStyleGuideColor.MainText, mainModifier), "Spring", 40),

		Size = UDim2.new(1, 0, 0, 0), -- Initial size
		AutomaticSize = Enum.AutomaticSize.Y -- Enable automatic height adjustment
	}

	local hydrateProps = stripProps(props, COMPONENT_ONLY_PROPERTIES)
	return Hydrate(newLabel)(hydrateProps)
end
