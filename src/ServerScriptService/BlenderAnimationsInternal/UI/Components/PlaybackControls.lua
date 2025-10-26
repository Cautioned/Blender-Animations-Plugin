--!native
--!strict
--!optimize 2

local State = require(script.Parent.Parent.Parent.state)
local Fusion = require(script.Parent.Parent.Parent.Packages.Fusion)

local New = Fusion.New
local Children = Fusion.Children
local OnChange = Fusion.OnChange
local OnEvent = Fusion.OnEvent
local Value = Fusion.Value
local Computed = Fusion.Computed

local StudioComponents = script.Parent.Parent.Parent.Components:FindFirstChild("StudioComponents")
local Checkbox = require(StudioComponents.Checkbox)
local Label = require(StudioComponents.Label)
local Slider = require(StudioComponents.Slider)
local VerticalCollapsibleSection = require(StudioComponents.VerticalCollapsibleSection)

local StudioComponentsUtil = StudioComponents:FindFirstChild("Util")
local themeProvider = require(StudioComponentsUtil.themeProvider)

local PlaybackControls = {}

function PlaybackControls.createPlaybackScrubber(services: any)
	return New("Frame")({
		Size = UDim2.new(1, 0, 0, 25),
		BackgroundColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.MainText) :: any,
		BackgroundTransparency = 0.9,
		LayoutOrder = 8,
		[Children] = {
			New("UIListLayout")({
				FillDirection = Enum.FillDirection.Horizontal,
				VerticalAlignment = Enum.VerticalAlignment.Center,
				SortOrder = Enum.SortOrder.LayoutOrder,
			}),
			Slider({
				Step = 0.01,
				Min = 0,
				Max = State.animationLength,
				Value = Computed(function()
					local playhead = State.playhead:get()
					return math.floor(playhead * 100) / 100
				end),
				OnChange = function(value)
					State.playhead:set(value)
					if services and services.playbackService then
						services.playbackService:onSliderChange(value)
					end
					return nil
				end,
				[OnEvent("InputBegan")] = function(input)
					if input.UserInputType == Enum.UserInputType.MouseButton1 then
						State.userChangingSlider:set(true)
					end
				end,
				[OnEvent("InputEnded")] = function(input)
					if input.UserInputType == Enum.UserInputType.MouseButton1 then
						State.userChangingSlider:set(false)
					end
				end,
				[Children] = Computed(function()
					local keyframes = State.keyframeNames:get()
					local animLength = State.animationLength:get()
					
					local indicators = {}
					for _, keyframe in ipairs(keyframes) do
						table.insert(
							indicators,
							New("Frame")({
								Size = UDim2.new(0, 2, 1, 0),
								Position = UDim2.new(
									(keyframe :: any).time / (animLength or 1),
									0,
									0,
									0
								),
								BackgroundColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.ErrorText) :: any,
								BorderSizePixel = 0,
							})
						)
					end
					return indicators
				end),
			}),
		},
	})
end

function PlaybackControls.createPlaybackSection(services: any)
	return VerticalCollapsibleSection({
		Text = "Playback",
		Collapsed = false,
		LayoutOrder = 2,
		[Children] = {
			PlaybackControls.createPlaybackScrubber(services),
			New("Frame")({
				Size = UDim2.new(1, 0, 0, 40),
				BackgroundTransparency = 1,
				LayoutOrder = 9,
				[Children] = {
					New("UIListLayout")({
						FillDirection = Enum.FillDirection.Horizontal,
						Padding = UDim.new(0, 5),
						HorizontalAlignment = Enum.HorizontalAlignment.Center,
						VerticalAlignment = Enum.VerticalAlignment.Center,
						SortOrder = Enum.SortOrder.LayoutOrder,
					}),
					New("ImageButton")({
						Image = "rbxasset://textures/AnimationEditor/button_control_previous.png",
						Size = UDim2.new(0, 40, 0, 40),
						BackgroundTransparency = 1,
						ImageColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.MainText) :: any,
						LayoutOrder = 1,
						[OnEvent("Activated")] = function()
							State.isPlaying:set(false)
							if services and services.playbackService then
								services.playbackService:seekAnimationToTime(0)
							end
						end,
					}),
					New("ImageButton")({
						Image = State.reversePlayPauseButtonImage,
						Size = UDim2.new(0, 40, 0, 40),
						BackgroundTransparency = 1,
						ImageColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.MainText) :: any,
						LayoutOrder = 2,
						[OnEvent("Activated")] = function()
							if services and services.playbackService then
								services.playbackService:onReverseButtonActivated()
							end
						end,
					}),
					New("ImageButton")({
						Image = State.playPauseButtonImage,
						Size = UDim2.new(0, 40, 0, 40),
						BackgroundTransparency = 1,
						ImageColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.MainText) :: any,
						LayoutOrder = 3,
						[OnEvent("Activated")] = function()
							if services and services.playbackService then
								services.playbackService:onPlayPauseButtonActivated()
							end
						end,
					}),
					New("ImageButton")({
						Image = "rbxasset://textures/AnimationEditor/button_control_next.png",
						Size = UDim2.new(0, 40, 0, 40),
						BackgroundTransparency = 1,
						ImageColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.MainText) :: any,
						LayoutOrder = 4,
						[OnEvent("Activated")] = function()
							State.isPlaying:set(false)
							if services and services.playbackService then
								services.playbackService:seekAnimationToTime(State.animationLength:get() - 0.001)
							end
						end,
					}),
				},
			}),
			Checkbox({
				Value = State.loopAnimation,
				Text = "Loop Animation",
				LayoutOrder = 10,
				OnChange = function(newValue: boolean): nil
					State.loopAnimation:set(newValue)
					return nil
				end,
			}),
			Label({
				LayoutOrder = 11,
				Text = Computed(function()
					local playhead = State.playhead:get()
					local currentFrame = math.floor(playhead * 60 + 0.5)
					return string.format("Frame: %d", currentFrame)
				end),
			}),
		},
	})
end

return PlaybackControls
