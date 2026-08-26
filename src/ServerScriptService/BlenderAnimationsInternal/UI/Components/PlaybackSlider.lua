--!native
--!strict
--!optimize 2

local Fusion = require(script.Parent.Parent.Parent.Packages.Fusion)

local New = Fusion.New
local Children = Fusion.Children
local Computed = Fusion.Computed
local Cleanup = Fusion.Cleanup
local OnEvent = Fusion.OnEvent
local Observer = Fusion.Observer
local Value = Fusion.Value
local Ref = Fusion.Ref

local StudioComponentsUtil = script.Parent.Parent.Parent.Components.StudioComponents:FindFirstChild("Util")
local getDragInput = require(StudioComponentsUtil.getDragInput)
local getMotionState = require(StudioComponentsUtil.getMotionState)
local getState = require(StudioComponentsUtil.getState)
local themeProvider = require(StudioComponentsUtil.themeProvider)
local unwrap = require(StudioComponentsUtil.unwrap)

local PlaybackSlider = {}

function PlaybackSlider.create(props: {
	Enabled: any?,
	LayoutOrder: number?,
	Max: any?,
	Min: any?,
	OnChange: ((number) -> nil)?,
	Size: UDim2?,
	Step: any?,
	Value: any?,
})
	local isEnabled = getState(props.Enabled, true)
	local isHovering = Value(false)
	local handleRegion = Value()
	local inputValue = getState(props.Value, 0)
	local visualAlpha = Value(0)
	local lastAlpha = 0
	local isWrapping = Value(false)
	local progressAlpha = Computed(function()
		local minValue = unwrap(props.Min) or 0
		local maxValue = unwrap(props.Max) or 1
		local value = unwrap(props.Value) or minValue
		local range = maxValue - minValue
		if range <= 1e-8 then
			return 0
		end
		return math.clamp((value - minValue) / range, 0, 1)
	end)

	local currentValue, currentAlpha, isDragging = getDragInput({
		Instance = handleRegion,
		Enabled = isEnabled,
		Value = Value(Vector2.new(unwrap(inputValue), 0)),
		Min = Computed(function()
			return Vector2.new(unwrap(props.Min) or 0, 0)
		end),
		Max = Computed(function()
			return Vector2.new(unwrap(props.Max) or 1, 0)
		end),
		Step = Computed(function()
			return Vector2.new(unwrap(props.Step) or -1, 0)
		end),
		OnChange = function(newValue: Vector2)
			if props.OnChange then
				props.OnChange(newValue.X)
			end
		end,
	})

	local cleanupDraggingObserver = Observer(isDragging):onChange(function()
		inputValue:set(unwrap(currentValue).X)
	end)

	local cleanupInputObserver = Observer(inputValue):onChange(function()
		if not unwrap(isDragging) then
			currentValue:set(Vector2.new(unwrap(inputValue, 0), 0))
		end
	end)

	local cleanupProgressObserver = Observer(progressAlpha):onChange(function()
		local nextAlpha = progressAlpha:get()
		if lastAlpha > 0.85 and nextAlpha < 0.15 then
			isWrapping:set(true)
			visualAlpha:set(nextAlpha)
			task.defer(function()
				isWrapping:set(false)
			end)
		else
			visualAlpha:set(nextAlpha)
		end
		lastAlpha = nextAlpha
	end)

	visualAlpha:set(progressAlpha:get())
	lastAlpha = progressAlpha:get()

	return New("Frame")({
		Name = "PlaybackSlider",
		Size = props.Size or UDim2.new(1, 0, 0, 18),
		LayoutOrder = props.LayoutOrder,
		BackgroundTransparency = 1,
		[Cleanup] = function()
			cleanupDraggingObserver()
			cleanupInputObserver()
			cleanupProgressObserver()
		end,
		[Children] = {
			New("Frame")({
				Name = "Track",
				AnchorPoint = Vector2.new(0.5, 0.5),
				Position = UDim2.fromScale(0.5, 0.5),
				Size = UDim2.new(1, -16, 0, 7),
				BackgroundColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.InputFieldBackground),
				BackgroundTransparency = 0.35,
				BorderSizePixel = 0,
				[Children] = {
					New("UICorner")({
						CornerRadius = UDim.new(1, 0),
					}),
					New("Frame")({
						Name = "Fill",
						Size = Computed(function()
							return UDim2.new(visualAlpha:get(), 0, 1, 0)
						end),
						BackgroundColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.ScriptInformation),
						BorderSizePixel = 0,
						[Children] = New("UICorner")({
							CornerRadius = UDim.new(1, 0),
						}),
					}),
					New("Frame")({
						Name = "ProgressCap",
						AnchorPoint = Vector2.new(0.5, 0.5),
						Position = Computed(function()
							return UDim2.new(visualAlpha:get(), 0, 0.5, 0)
						end),
						Size = UDim2.fromOffset(8, 8),
						BackgroundColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.ScriptInformation),
						BackgroundTransparency = 0.15,
						BorderSizePixel = 0,
						[Children] = New("UICorner")({
							CornerRadius = UDim.new(1, 0),
						}),
					}),
					-- forward any children (eg. keyframe marker indicators) into the track
					props.Children,
				},
			}),
			New("Frame")({
				Name = "HandleRegion",
				BackgroundTransparency = 1,
				Position = UDim2.fromOffset(8, 0),
				Size = UDim2.new(1, -16, 1, 0),
				[Ref] = handleRegion,
				[Children] = {
					New("Frame")({
						Name = "Handle",
						AnchorPoint = Vector2.new(0.5, 0.5),
						Position = Computed(function()
							return UDim2.fromScale(visualAlpha:get(), 0.5)
						end),
						Size = getMotionState(Computed(function()
							local offset = if isHovering:get() then 16 else 14
							return UDim2.fromOffset(offset, offset)
						end), "Spring", 40),
						BackgroundColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.MainText),
						BorderSizePixel = 0,
						[Children] = {
							New("UICorner")({
								CornerRadius = UDim.new(1, 0),
							}),
							New("UIStroke")({
								Color = themeProvider:GetColor(Enum.StudioStyleGuideColor.InputFieldBorder),
								Thickness = 1,
								Transparency = 0.15,
							}),
						},
						[OnEvent("InputBegan")] = function(inputObject)
							if inputObject.UserInputType == Enum.UserInputType.MouseMovement then
								isHovering:set(true)
							end
						end,
						[OnEvent("InputEnded")] = function(inputObject)
							if inputObject.UserInputType == Enum.UserInputType.MouseMovement then
								isHovering:set(false)
							end
						end,
					}),
				},
			}),
		},
	})
end

return PlaybackSlider
