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
local Observer = Fusion.Observer
local Spring = Fusion.Spring

local StudioComponents = script.Parent.Parent.Parent.Components:FindFirstChild("StudioComponents")
local Checkbox = require(StudioComponents.Checkbox)
local Label = require(StudioComponents.Label)
local Slider = require(StudioComponents.Slider)
local VerticalCollapsibleSection = require(StudioComponents.VerticalCollapsibleSection)

local StudioComponentsUtil = StudioComponents:FindFirstChild("Util")
local themeProvider = require(StudioComponentsUtil.themeProvider)

local PlaybackControls = {}

function PlaybackControls.createPlaybackScrubber(services: any)
	local sliderValue = Value(0)
	
	-- sync slider value with playhead when not being dragged
	local cleanupPlayheadObserver = Observer(State.playhead):onChange(function()
		if not State.userChangingSlider:get() then
			sliderValue:set(State.playhead:get())
		end
	end)
	
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
				Value = sliderValue,
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

function PlaybackControls.createPlaybackSection(services: any, layoutOrder: number?)
	local function createPlaybackButton(props: { [any]: any })
		return (function()
			local isHovering = Value(false)
			local isPressed = Value(false)
			return New("ImageButton")({
				Image = props.Image,
				Size = props.Size or UDim2.new(0, 40, 0, 40),
				BackgroundTransparency = 1,
				ImageColor3 = props.ImageColor3,
				LayoutOrder = props.LayoutOrder,
				[Children] = {
					New("UIScale")({
						Scale = Spring(Computed(function()
							if State.reducedMotion:get() then
								return 1
							end
							if isPressed:get() then
								return 0.97
							end
							if isHovering:get() then
								return 1.02
							end
							return 1
						end), 25, 0.9),
					}),
				},
				[OnEvent("MouseEnter")] = function()
					isHovering:set(true)
				end,
				[OnEvent("MouseLeave")] = function()
					isHovering:set(false)
					isPressed:set(false)
				end,
				[OnEvent("InputBegan")] = function(input)
					if input.UserInputType == Enum.UserInputType.MouseButton1 then
						isPressed:set(true)
					end
				end,
				[OnEvent("InputEnded")] = function(input)
					if input.UserInputType == Enum.UserInputType.MouseButton1 then
						isPressed:set(false)
					end
				end,
				[OnEvent("Activated")] = props.Activated,
			})
		end)()
	end

	return VerticalCollapsibleSection({
		Text = "Playback",
		Collapsed = false,
		LayoutOrder = layoutOrder or 2,
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
					createPlaybackButton({
						Image = "rbxasset://textures/AnimationEditor/button_control_previous.png",
						ImageColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.MainText) :: any,
						LayoutOrder = 1,
						Activated = function()
							if services and services.playbackService then
								services.playbackService:seekAnimationToTime(0)
								State.isPlaying:set(false)
								-- Stop the animation track to prevent it from continuing
								if services.playbackService.State.currentAnimTrack then
									(services.playbackService.State.currentAnimTrack :: AnimationTrack):AdjustSpeed(0)
								end
								services.playbackService:updateUI()
							end
						end,
					}),
					createPlaybackButton({
						Image = State.reversePlayPauseButtonImage,
						ImageColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.MainText) :: any,
						LayoutOrder = 2,
						Activated = function()
							if services and services.playbackService then
								services.playbackService:onReverseButtonActivated()
							end
						end,
					}),
					createPlaybackButton({
						Image = State.playPauseButtonImage,
						ImageColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.MainText) :: any,
						LayoutOrder = 3,
						Activated = function()
							if services and services.playbackService then
								services.playbackService:onPlayPauseButtonActivated()
							end
						end,
					}),
					createPlaybackButton({
						Image = "rbxasset://textures/AnimationEditor/button_control_next.png",
						ImageColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.MainText) :: any,
						LayoutOrder = 4,
						Activated = function()
							if services and services.playbackService then
								-- Use the actual animation track length for accurate seeking
								local animLength = 0
								if services.playbackService.State.currentAnimTrack then
									animLength = (services.playbackService.State.currentAnimTrack :: AnimationTrack).Length
								else
									animLength = State.animationLength:get()
								end
								services.playbackService:seekAnimationToTime(animLength - 0.001)
								State.isPlaying:set(false)
								-- Stop the animation track to prevent it from continuing
								if services.playbackService.State.currentAnimTrack then
									(services.playbackService.State.currentAnimTrack :: AnimationTrack):AdjustSpeed(0)
								end
								services.playbackService:updateUI()
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
