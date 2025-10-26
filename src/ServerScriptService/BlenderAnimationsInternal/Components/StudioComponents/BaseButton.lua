-- Roact version by @sircfenner
-- Ported to Fusion by @YasuYoshida

local Plugin = script:FindFirstAncestorWhichIsA("Plugin")
local Fusion = require(Plugin:FindFirstChild("Fusion", true))

local StudioComponents = script.Parent
local StudioComponentsUtil = StudioComponents:FindFirstChild("Util")

local BoxBorder = require(StudioComponents.BoxBorder)

local getMotionState = require(StudioComponentsUtil.getMotionState)
local themeProvider = require(StudioComponentsUtil.themeProvider)
local getModifier = require(StudioComponentsUtil.getModifier)
local stripProps = require(StudioComponentsUtil.stripProps)
local constants = require(StudioComponentsUtil.constants)
local getState = require(StudioComponentsUtil.getState)
local unwrap = require(StudioComponentsUtil.unwrap)
local types = require(StudioComponentsUtil.types)

local New = Fusion.New
local Value = Fusion.Value
local Children = Fusion.Children
local Computed = Fusion.Computed
local OnEvent = Fusion.OnEvent
local Hydrate = Fusion.Hydrate

local COMPONENT_ONLY_PROPERTIES = {
	"TextColorStyle",
	"BackgroundColorStyle",
	"BorderColorStyle",
	"Activated",
	"Enabled",
}

type styleGuideColorInput = (Enum.StudioStyleGuideColor | types.StateObject<Enum.StudioStyleGuideColor>)?

export type BaseButtonProperties = {
	Activated: (() -> nil)?,
	Enabled: (boolean | types.StateObject<boolean>)?,
	TextColorStyle: styleGuideColorInput,
	BackgroundColorStyle: styleGuideColorInput,
	BorderColorStyle: styleGuideColorInput,
	[any]: any,
}

return function(props: BaseButtonProperties): TextButton
	local isEnabled = getState(props.Enabled, true)
	local isHovering = Value(false)
	local isPressed = Value(false)

	local modifier = getModifier({
		Enabled = isEnabled,
		Selected = props.Selected,
		Pressed = isPressed,
		Hovering = isHovering,
	})

	local newBaseButton = BoxBorder {
		Color = getMotionState(themeProvider:GetColor(props.BorderColorStyle or Enum.StudioStyleGuideColor.CheckedFieldBorder, modifier), "Spring", 40),

		[Children] = New "TextButton" {
			Name = "BaseButton",
			Size = UDim2.fromScale(1, 1),
			Text = "Button",
			Font = themeProvider:GetFont("Default"),
			TextSize = constants.TextSize,
			TextColor3 = getMotionState(themeProvider:GetColor(props.TextColorStyle or Enum.StudioStyleGuideColor.ButtonText, modifier), "Spring", 40),
			BackgroundColor3 = getMotionState(themeProvider:GetColor(props.BackgroundColorStyle or Enum.StudioStyleGuideColor.Button, modifier), "Spring", 40),
			AutoButtonColor = false,
			TextWrapped = true,

			[OnEvent "InputBegan"] = function(inputObject)
				if not unwrap(isEnabled) then
					return
				elseif inputObject.UserInputType == Enum.UserInputType.MouseMovement then
					isHovering:set(true)
				elseif inputObject.UserInputType == Enum.UserInputType.MouseButton1 then
					isPressed:set(true)
				end
			end,
			[OnEvent "InputEnded"] = function(inputObject)
				if not unwrap(isEnabled) then
					return
				elseif inputObject.UserInputType == Enum.UserInputType.MouseMovement then
					isHovering:set(false)
				elseif inputObject.UserInputType == Enum.UserInputType.MouseButton1 then
					isPressed:set(false)
				end
			end,
			[OnEvent "Activated"] = (function()
				if props.Activated then
					return function()
						if unwrap(isEnabled, false) then
							props.Activated()
						end
					end
				end
			end)(),
		}
	}

	local hydrateProps = stripProps(props, COMPONENT_ONLY_PROPERTIES)
	return Hydrate(newBaseButton)(hydrateProps)
end