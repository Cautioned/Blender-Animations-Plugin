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

local Computed = Fusion.Computed
local OnChange = Fusion.OnChange
local Children = Fusion.Children
local Hydrate = Fusion.Hydrate
local OnEvent = Fusion.OnEvent
local Value = Fusion.Value
local Out = Fusion.Out
local New = Fusion.New

local PLACEHOLDER_TEXT_COLOR = Color3.fromRGB(102, 102, 102)

local COMPONENT_ONLY_PROPERTIES = {
	"Enabled",
	"ClearTextOnFocus"
}

export type TextInputProperties = {
	Enabled: (boolean | types.StateObject<boolean>)?,
	[any]: any,
}

return function(props: TextInputProperties): TextLabel
	local isEnabled = getState(props.Enabled, true)
	local isHovering = Value(false)
	local isFocused = Value(false)

	local mainModifier = getModifier({
		Enabled = isEnabled,
	})
	
	local borderModifier = getModifier({
		Enabled = isEnabled,
		Selected = isFocused,
		Hovering = isHovering,
	})

	local currentTextBounds = Value(Vector2.zero)
	local absoluteTextBoxSize = Value(Vector2.zero)

	local newTextBox = BoxBorder {
		Color = getMotionState(themeProvider:GetColor(Enum.StudioStyleGuideColor.InputFieldBorder, borderModifier), "Spring", 40),

		[Children] = New "TextBox" {
			Name = "TextInput",
			Size = UDim2.new(1, 0, 0, 25),
			Text = "",
			TextSize = constants.TextSize,
			PlaceholderColor3 = PLACEHOLDER_TEXT_COLOR,
			ClipsDescendants = true,

			Font = themeProvider:GetFont("Default"),
			BackgroundColor3 = getMotionState(themeProvider:GetColor(Enum.StudioStyleGuideColor.InputFieldBackground, mainModifier), "Spring", 40),
			TextColor3 = getMotionState(themeProvider:GetColor(Enum.StudioStyleGuideColor.MainText, mainModifier), "Spring", 40),
			TextEditable = isEnabled,

			TextXAlignment = Computed(function()
				local bounds = (unwrap(currentTextBounds) or Vector2.zero).X + 5 -- because of padding
				local pixels = (unwrap(absoluteTextBoxSize) or Vector2.zero).X
				return if bounds >= pixels then Enum.TextXAlignment.Right else Enum.TextXAlignment.Left
			end),

			ClearTextOnFocus = Computed(function()
				local clearTextOnFocus = (unwrap(props.ClearTextOnFocus) or false)
				local isEnabled = unwrap(isEnabled)
				return clearTextOnFocus and isEnabled
			end),

			[Out "TextBounds"] = currentTextBounds,
			[Out "AbsoluteSize"] = absoluteTextBoxSize,

			[OnEvent "Focused"] = function() isFocused:set(true) end,
			[OnEvent "FocusLost"] = function() isFocused:set(false) end,

			[OnEvent "InputBegan"] = function(inputObject)
				if not unwrap(isEnabled) then
					return
				elseif inputObject.UserInputType == Enum.UserInputType.MouseMovement then
					isHovering:set(true)
				end
			end,
			
			[OnEvent "InputEnded"] = function(inputObject)
				if not unwrap(isEnabled) then
					return
				elseif inputObject.UserInputType == Enum.UserInputType.MouseMovement then
					isHovering:set(false)
				end
			end,

			[Children] = New "UIPadding" {
				PaddingLeft = UDim.new(0, 5),
				PaddingRight = UDim.new(0, 5),
			},
		}
	}

	local hydrateProps = stripProps(props, COMPONENT_ONLY_PROPERTIES)
	return Hydrate(newTextBox)(hydrateProps)
end