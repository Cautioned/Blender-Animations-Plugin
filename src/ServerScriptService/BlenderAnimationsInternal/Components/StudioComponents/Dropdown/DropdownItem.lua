local Plugin = script:FindFirstAncestorWhichIsA("Plugin")
local Fusion = require(Plugin:FindFirstChild("Fusion", true))

local StudioComponents = script.Parent.Parent
local StudioComponentsUtil = StudioComponents:FindFirstChild("Util")

local themeProvider = require(StudioComponentsUtil.themeProvider)
local getModifier = require(StudioComponentsUtil.getModifier)
local constants = require(StudioComponentsUtil.constants)
local getState = require(StudioComponentsUtil.getState)
local unwrap = require(StudioComponentsUtil.unwrap)

local dropdownConstants = require(script.Parent.Constants)

local Computed = Fusion.Computed
local Children = Fusion.Children
local OnEvent = Fusion.OnEvent
local Hydrate = Fusion.Hydrate
local Value = Fusion.Value
local New = Fusion.New

local COMPONENT_ONLY_PROPERTIES = {
	"OnSelected",
	"Enabled",
	"Item"
}

type DropdownItemProperties = {
	OnSelected: ((selectedOption: any) -> nil),
	Item: any,
	[any]: any,
}

return function(props: DropdownItemProperties): TextButton
	local isEnabled = getState(props.Enabled, true)
	local isHovering = Value(false)

	local modifier = getModifier({
		Enabled = isEnabled,
		Hovering = isHovering,
	})
	
	local newDropdownItem = New "TextButton" {
		AutoButtonColor = false,
		Name = "DropdownItem",
		Size = UDim2.new(1, 0, 0, 15),
		BackgroundColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.EmulatorBar, modifier),
		BorderSizePixel = 0,
		Font = themeProvider:GetFont("Default"),
		Text = tostring(props.Item),
		TextSize = constants.TextSize,
		TextColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.MainText, modifier),
		TextXAlignment = Enum.TextXAlignment.Left,
		TextTruncate = Enum.TextTruncate.AtEnd,
		
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
		[OnEvent "Activated"] = function()
			props.OnSelected(props.Item)
		end,
		
		[Children] = {
			New "UIPadding" {
				PaddingLeft = UDim.new(0, dropdownConstants.TextPaddingLeft - 1),
				PaddingRight = UDim.new(0, dropdownConstants.TextPaddingRight),
			},
			Computed(function()
				if unwrap(constants.CurvedBoxes, false) then
					return New "UICorner" {
						CornerRadius = constants.CornerRadius
					}
				end
			end, Fusion.cleanup)
		}
	}
	
	local hydrateProps = table.clone(props)
	for _,propertyIndex in pairs(COMPONENT_ONLY_PROPERTIES) do
		hydrateProps[propertyIndex] = nil
	end
	
	return Hydrate(newDropdownItem)(hydrateProps)
end