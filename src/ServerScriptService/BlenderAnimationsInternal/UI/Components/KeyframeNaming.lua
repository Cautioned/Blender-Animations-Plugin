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

local StudioComponents = script.Parent.Parent.Parent.Components:FindFirstChild("StudioComponents")
local Button = require(StudioComponents.Button)
local Label = require(StudioComponents.Label)
local TextInput = require(StudioComponents.TextInput)
local VerticalCollapsibleSection = require(StudioComponents.VerticalCollapsibleSection)

local StudioComponentsUtil = StudioComponents:FindFirstChild("Util")
local themeProvider = require(StudioComponentsUtil.themeProvider)

local KeyframeNaming = {}

function KeyframeNaming.addKeyframeName()
	local currentKeyframes = State.keyframeNames:get()
	table.insert(currentKeyframes, { name = State.keyframeNameInput:get(), time = State.playhead:get() })
	table.sort(currentKeyframes, function(a, b)
		return a.time < b.time
	end)

	State.keyframeNames:set(currentKeyframes)
	State.keyframeNameInput:set("Name")
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
				Text = "Name Marker/Event at Current Time",
				LayoutOrder = 1,
			}),
			TextInput({
				PlaceholderText = "Keyframe Name",
				Text = State.keyframeNameInput,
				LayoutOrder = 2,
				[OnChange("Text")] = function(newText)
					State.keyframeNameInput:set(newText)
				end,
			}),
			New("Frame")({
				Size = UDim2.new(1, 0, 0, 30),
				LayoutOrder = 3,
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
				BackgroundTransparency = 1,
				[Children] = Computed(function()
					local keyframesUI = {}

					for index, keyframe in ipairs(State.keyframeNames:get()) do
						table.insert(
							keyframesUI,
							New("Frame")({
								Size = UDim2.new(1, 0, 0, 30),
								LayoutOrder = 4 + index,
								BackgroundTransparency = 0.9,
								[Children] = {
									New("TextLabel")({
										Text = (keyframe :: any).name .. " (" .. string.format(
											"%.2f",
											(keyframe :: any).time
										) .. "s)" .. " Frame : " .. math.floor(
											(keyframe :: any).time * 60 + 0.5
										),
										Size = UDim2.new(0.7, 0, 1, 0),
										Position = UDim2.new(0, 0, 0, 0),
										BackgroundTransparency = 0.9,
										TextXAlignment = Enum.TextXAlignment.Left,
										TextColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.MainText) :: any,
										BackgroundColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.MainText) :: any,
									}),
									New("TextButton")({
										Text = "Remove",
										Size = UDim2.new(0.3, 0, 1, 0),
										Position = UDim2.new(0.7, 0, 0, 0),
										BackgroundTransparency = 0.9,
										TextColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.MainText) :: any,
										BackgroundColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.MainText) :: any,
										[OnEvent("Activated")] = function()
											KeyframeNaming.removeKeyframeName(index)
										end,
									}),
								},
							})
						)
					end
					return keyframesUI
				end),
			}) :: any,
		},
	})
end

return KeyframeNaming
