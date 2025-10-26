-- Roact version by @sircfenner
-- Ported to Fusion by @YasuYoshida

local Plugin = script:FindFirstAncestorWhichIsA("Plugin")
local Fusion = require(Plugin:FindFirstChild("Fusion", true))

local StudioComponents = script.Parent
local StudioComponentsUtil = StudioComponents:FindFirstChild("Util")

local BoxBorder = require(StudioComponents.BoxBorder)

local getMotionState = require(StudioComponentsUtil.getMotionState)
local themeProvider = require(StudioComponentsUtil.themeProvider)
local getDragInput = require(StudioComponentsUtil.getDragInput)
local getModifier = require(StudioComponentsUtil.getModifier)
local stripProps = require(StudioComponentsUtil.stripProps)
local getState = require(StudioComponentsUtil.getState)
local unwrap = require(StudioComponentsUtil.unwrap)
local types = require(StudioComponentsUtil.types)

local Computed = Fusion.Computed
local Children = Fusion.Children
local Observer = Fusion.Observer
local OnEvent = Fusion.OnEvent
local Hydrate = Fusion.Hydrate
local Cleanup = Fusion.Cleanup
local Value = Fusion.Value
local Out = Fusion.Out
local New = Fusion.New
local Ref = Fusion.Ref

local COMPONENT_ONLY_PROPERTIES = {
	"ZIndex",
	"HandleSize",
	"OnChange",
	"Value",
	"Min",
	"Max",
	"Step",
	"Enabled",
}

type numberInput = types.CanBeState<number>?

type SliderProperties = {
	HandleSize: types.CanBeState<UDim2>?,
	Enabled: types.CanBeState<boolean>?,
	OnChange: ((newValue: number) -> nil)?,
	Value: types.CanBeState<number>?,
	Min: numberInput,
	Max: numberInput,
	Step: numberInput,
	[any]: any,
}

return function(props: SliderProperties): TextButton
	local isEnabled = getState(props.Enabled, true)
	local isHovering = Value(false)
	
	local handleSize = props.HandleOffsetSize or UDim2.new(0, 12, 1, -2)

	local handleRegion = Value()
	local inputValue = getState(props.Value, 1)
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

	local cleanupInputValueObserver = Observer(inputValue):onChange(function()
		currentValue:set(Vector2.new(unwrap(inputValue, false), 0))
	end)

	local function cleanupCallback()
		cleanupDraggingObserver()
		cleanupInputValueObserver()
	end

	local zIndex = Computed(function()
		return (unwrap(props.ZIndex) or 0) + 1
	end)

	local mainModifier = getModifier({
		Enabled = isEnabled,
	})
	
	local handleModifier = getModifier({
		Enabled = isEnabled,
		Selected = isDragging,
		Hovering = isHovering,
	})

	local handleFill = themeProvider:GetColor(Enum.StudioStyleGuideColor.Button)
	local handleBorder = themeProvider:GetColor(Enum.StudioStyleGuideColor.InputFieldBorder, handleModifier)
	local barAbsSize = Value(Vector2.zero)
	
	local newSlider = New "Frame" {
		Name = "Slider",
		Size = UDim2.new(1, 0, 0, 22),
		ZIndex = zIndex,
		BackgroundTransparency = 1,
		[Cleanup] = cleanupCallback,

		[Children] = {
			BoxBorder {
				Color = themeProvider:GetColor(Enum.StudioStyleGuideColor.InputFieldBorder),
				CornerRadius = UDim.new(0, 1),
				
				[Children] = New "Frame" {
					Name = "Bar",
					ZIndex = zIndex,
					Position = UDim2.fromScale(.5, .5),
					AnchorPoint = Vector2.new(.5, .5),
					BorderSizePixel = 0,
					
					[Out "AbsoluteSize"] = barAbsSize,
					
					Size = Computed(function()
						local handleSize = unwrap(handleSize) or UDim2.new()
						return UDim2.new(1, -handleSize.X.Offset, 0, 5)
					end),

					BackgroundColor3 = getMotionState(themeProvider:GetColor(Enum.StudioStyleGuideColor.InputFieldBackground, mainModifier), "Spring", 40),

					BackgroundTransparency = getMotionState(Computed(function()
						return if not unwrap(isEnabled) then 0.4 else 0
					end), "Spring", 40),
				}
			},
			New "Frame" {
				Name = "HandleRegion",
				ZIndex = 1,
				Size = UDim2.new(1, 0, 1, 0),
				BackgroundTransparency = 1,
				[Ref] = handleRegion,

				[Children] = BoxBorder {
					Color =  getMotionState(Computed(function()
						return unwrap(handleBorder):Lerp(unwrap(handleFill), if not unwrap(isEnabled) then .5 else 0)
					end), "Spring", 40),

					[Children] = New "Frame" {
						Name = "Handle",
						BorderMode = Enum.BorderMode.Inset,
						BackgroundColor3 = handleFill,
						BorderSizePixel = 0,
						
						Size = handleSize,
						
						AnchorPoint = Vector2.new(.5, .5),

						Position = getMotionState(Computed(function()
							local handleSize = unwrap(handleSize) or UDim2.new()
							local absoluteBarSize = unwrap(barAbsSize) or Vector2.zero
							return UDim2.new(
								0, (unwrap(currentAlpha).X*absoluteBarSize.X) + handleSize.X.Offset/2,
								.5, 0
							)
						end), "Spring", 40),

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
					}
				}
			}
		}
	}

	local hydrateProps = stripProps(props, COMPONENT_ONLY_PROPERTIES)
	return Hydrate(newSlider)(hydrateProps)
end