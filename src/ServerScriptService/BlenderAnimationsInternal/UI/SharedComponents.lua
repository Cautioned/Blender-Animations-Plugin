--!native
--!strict
--!optimize 2

local State = require(script.Parent.Parent.state)
local Fusion = require(script.Parent.Parent.Packages.Fusion)

local New = Fusion.New
local Children = Fusion.Children
local OnChange = Fusion.OnChange
local OnEvent = Fusion.OnEvent
local Value = Fusion.Value
local Computed = Fusion.Computed
local Observer = Fusion.Observer
local Spring = Fusion.Spring

local StudioComponents = script.Parent.Parent.Components:FindFirstChild("StudioComponents")
local Label = require(StudioComponents.Label)
local StudioComponentsUtil = StudioComponents:FindFirstChild("Util")
local themeProvider = require(StudioComponentsUtil.themeProvider)

local SharedComponents = {}

function SharedComponents.AnimatedHintLabel(props: {
	Text: any,
	Size: UDim2,
	TextWrapped: boolean,
	LayoutOrder: number,
	ClipsDescendants: boolean,
	Visible: boolean,
	TextTransparency: any,
})
	local activeHint = props.Text
	local displayedText = Value(activeHint:get())

	Observer(activeHint):onChange(function()
		if activeHint:get() ~= "" then
			displayedText:set(activeHint:get())
		end
	end)

	local isVisible = Computed(function()
		return activeHint:get() ~= ""
	end)

	local height = Spring(Computed(function()
		return isVisible:get() and 50 or 0
	end), 20)

	local transparency = Spring(Computed(function()
		return isVisible:get() and 0 or 1
	end), 20)

	return Label({
		Text = displayedText,
		TextWrapped = true,
		Size = Computed(function()
			return UDim2.new(1, 0, 0, height:get())
		end),
		LayoutOrder = props.LayoutOrder,
		TextTransparency = transparency,
		ClipsDescendants = true,
	})
end

function SharedComponents.createHeaderUI()
	return New("Frame")({
		LayoutOrder = 0,
		Size = UDim2.new(1, 0, 0, 0),
		AutomaticSize = Enum.AutomaticSize.Y,
		BackgroundTransparency = 1,
		[Children] = {
			New("UIListLayout")({
				SortOrder = Enum.SortOrder.LayoutOrder,
				Padding = UDim.new(0, 4),
			}),
			New("UIPadding")({
				PaddingLeft = UDim.new(0, 5),
				PaddingRight = UDim.new(0, 5),
			}),
			Label({
				LayoutOrder = 1,
				Text = Computed(function()
					return "Rig: " .. State.rigModelName:get()
				end),
				Font = Enum.Font.SourceSansBold,
				TextXAlignment = Enum.TextXAlignment.Left,
			}),
			Label({
				LayoutOrder = 2,
				Text = Computed(function()
					local warnings = State.activeWarnings:get()
					return table.concat(warnings, "\n")
				end),
				Visible = Computed(function()
					return #State.activeWarnings:get() > 0
				end),
				TextColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.WarningText),
				TextXAlignment = Enum.TextXAlignment.Left,
				TextWrapped = true,
				Size = UDim2.new(1, 0, 0, 0),
				AutomaticSize = Enum.AutomaticSize.Y,
			}),
			New("UIPadding")({
				PaddingBottom = UDim.new(0, 5),
			}),
		},
	})
end

return SharedComponents
