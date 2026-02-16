--!native
--!strict
--!optimize 2

local State = require(script.Parent.Parent.Parent.state)
local Fusion = require(script.Parent.Parent.Parent.Packages.Fusion)

local New = Fusion.New
local Children = Fusion.Children
local OnChange = Fusion.OnChange
local OnEvent = Fusion.OnEvent
local Computed = Fusion.Computed
local Value = Fusion.Value

local StudioComponents = script.Parent.Parent.Parent.Components:FindFirstChild("StudioComponents")
local Button = require(StudioComponents.Button)
local Dropdown = require(StudioComponents.Dropdown)
local Label = require(StudioComponents.Label)
local TextInput = require(StudioComponents.TextInput)
local VerticalCollapsibleSection = require(StudioComponents.VerticalCollapsibleSection)

local StudioComponentsUtil = StudioComponents:FindFirstChild("Util")
local themeProvider = require(StudioComponentsUtil.themeProvider)

local KeyframeNaming = {}

function KeyframeNaming.addKeyframeName()
	local currentKeyframes = State.keyframeNames:get()
	local valueInput = State.keyframeValueInput:get()
	local markerType = State.keyframeMarkerType:get()
	table.insert(currentKeyframes, {
		name = State.keyframeNameInput:get(),
		time = State.playhead:get(),
		value = valueInput ~= "" and valueInput or nil,
		type = markerType
	})
	table.sort(currentKeyframes, function(a, b)
		return a.time < b.time
	end)

	State.keyframeNames:set(currentKeyframes)
	State.keyframeNameInput:set("Name")
	State.keyframeValueInput:set("")
end

function KeyframeNaming.removeKeyframeName(index)
	local currentKeyframes = State.keyframeNames:get()
	table.remove(currentKeyframes, index)
	State.keyframeNames:set(currentKeyframes)
end

function KeyframeNaming.createKeyframeNamingUI(services: any, layoutOrder: number?)
	return VerticalCollapsibleSection({
		Text = "Keyframe Naming / Markers / Events",
		Collapsed = false,
		LayoutOrder = layoutOrder or 14,
		[Children] = {
			Label({
				Text = "Insert Animation Event (marker), or Name Keyframe",
				LayoutOrder = 1,
			}),
			Dropdown({
				Options = { "Name", "Event" },
				Value = State.keyframeMarkerType,
				LayoutOrder = 2,
				OnSelected = function(newValue)
					State.keyframeMarkerType:set(newValue)
				end,
			}) :: any,
			TextInput({
				PlaceholderText = "Keyframe Name",
				Text = State.keyframeNameInput,
				LayoutOrder = 3,
				[OnChange("Text")] = function(newText)
					State.keyframeNameInput:set(newText)
				end,
			}),
			TextInput({
				PlaceholderText = "Value (Optional)",
				Text = State.keyframeValueInput,
				LayoutOrder = 4,
				Visible = Computed(function()
					return State.keyframeMarkerType:get() == "Event"
				end),
				[OnChange("Text")] = function(newText)
					State.keyframeValueInput:set(newText)
				end,
			}),
			New("Frame")({
				Size = UDim2.new(1, 0, 0, 30),
				LayoutOrder = 5,
				BackgroundTransparency = 1,
				[Children] = {
					Button({
						Text = "Add Marker/Event",
						Size = UDim2.new(1, 0, 1, 0),
						Activated = KeyframeNaming.addKeyframeName :: (() -> nil)?,
						Enabled = Computed(function()
							return State.activeRigExists:get()
						end),
					}),
				},
			}) :: any,
			New("Frame")({
				Size = UDim2.new(1, 0, 0, 0),
				AutomaticSize = Enum.AutomaticSize.Y,
				BackgroundTransparency = 1,
				[Children] = Computed(function()
					local keyframesUI = {
						New("UIListLayout")({
							SortOrder = Enum.SortOrder.LayoutOrder,
							Padding = UDim.new(0, 2),
						})
					}

					for index, keyframe in ipairs(State.keyframeNames:get()) do
						local markerType = (keyframe :: any).type or "Name"
						local displayText = "[" .. markerType .. "] " .. (keyframe :: any).name .. " (" .. string.format(
							"%.2f",
							(keyframe :: any).time
						) .. "s)" .. " Frame : " .. math.floor(
							(keyframe :: any).time * 60 + 0.5
						)
						
						if (keyframe :: any).value and (keyframe :: any).value ~= "" then
							displayText = displayText .. " | Value: " .. (keyframe :: any).value
						end
						
						local isHovering = Value(false)
						
						table.insert(
							keyframesUI,
							New("Frame")({
								Size = UDim2.new(1, 0, 0, 30),
								LayoutOrder = 6 + index,
								BackgroundTransparency = Computed(function()
									return isHovering:get() and 0.7 or 0.85
								end),
								BackgroundColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.InputFieldBackground) :: any,
								ClipsDescendants = true,
								[OnEvent("MouseEnter")] = function()
									isHovering:set(true)
								end,
								[OnEvent("MouseLeave")] = function()
									isHovering:set(false)
								end,
								[Children] = {
									New("UICorner")({
										CornerRadius = UDim.new(0, 4),
									}),
									New("TextLabel")({
										Text = displayText,
										Size = UDim2.new(0.7, -4, 1, 0),
										Position = UDim2.new(0, 4, 0, 0),
										BackgroundTransparency = 1,
										TextXAlignment = Enum.TextXAlignment.Left,
										TextColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.MainText) :: any,
									}),
									New("TextButton")({
										Text = "Remove",
										Size = UDim2.new(0.3, -4, 1, 0),
										Position = UDim2.new(0.7, 0, 0, 0),
										BackgroundTransparency = Computed(function()
											return isHovering:get() and 0.6 or 0.8
										end),
										BackgroundColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.ErrorText) :: any,
										TextColor3 = Color3.new(1, 1, 1),
										TextSize = 12,
										[OnEvent("Activated")] = function()
											KeyframeNaming.removeKeyframeName(index)
										end,
									}),
								},
							})
						)
					end
					return keyframesUI
				end, Fusion.cleanup),
			}) :: any,
		},
	})
end

return KeyframeNaming
