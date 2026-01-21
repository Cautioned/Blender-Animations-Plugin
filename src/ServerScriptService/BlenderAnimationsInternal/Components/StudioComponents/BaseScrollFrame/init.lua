-- Written by @boatbomber
-- Modified by @mvyasu

local Plugin = script:FindFirstAncestorWhichIsA("Plugin")
local Fusion = require(Plugin:FindFirstChild("Fusion", true))

local StudioComponents = script.Parent
local StudioComponentsUtil = StudioComponents:FindFirstChild("Util")

local Background = require(StudioComponents.Background)

local stripProps = require(StudioComponentsUtil.stripProps)
local getState = require(StudioComponentsUtil.getState)
local unwrap = require(StudioComponentsUtil.unwrap)
local types = require(StudioComponentsUtil.types)

local ScrollBar = require(script.ScrollBar)
local Children = Fusion.Children
local OnChange = Fusion.OnChange
local Computed = Fusion.Computed
local Hydrate = Fusion.Hydrate
local Value = Fusion.Value
local New = Fusion.New
local Ref = Fusion.Ref
local Out = Fusion.Out

local DEFAULT_SCROLL_BAR_THICKNESS = 14
local COMPONENT_ONLY_PROPERTIES = {
	"ScrollingEnabled",
	"VerticalScrollBarPosition",
	"VerticalScrollBarInset",
	"ScrollBarThickness",
	"ScrollBarBorderMode",
	"CanvasSize",
	"AutomaticCanvasSize",
	Children,
}

export type BaseScrollFrameProperties = {
	ScrollBarBorderMode:  types.CanBeState<Enum.BorderMode>?,
	CanvasSize: types.CanBeState<UDim2>?,
	ScrollingEnabled: types.CanBeState<boolean>?,
	ScrollBarThickness: types.CanBeState<number>?,
	VerticalScrollBarPosition: types.CanBeState<Enum.VerticalScrollBarPosition>?,
	VerticalScrollBarInset: types.CanBeState<Enum.ScrollBarInset>?,
	AutomaticCanvasSize: types.CanBeState<Enum.AutomaticSize>?,
	[any]: any,
}

return function(props: BaseScrollFrameProperties): Frame
	local isEnabled = getState(props.ScrollingEnabled, true)
	local vertPos = getState(props.VerticalScrollBarPosition, Enum.VerticalScrollBarPosition.Right)
	local vertInset = getState(props.VerticalScrollBarInset, Enum.ScrollBarInset.ScrollBar)
	
	local barVisibility = {
		Vertical = Value(false),
		Horizontal = Value(false),
	}
	
	local scrollBarThickness = Value(unwrap(props.ScrollBarThickness) or DEFAULT_SCROLL_BAR_THICKNESS)
	
	local canvasPosition = Value(Vector2.zero)
	local absCanvasSize = Value(Vector2.zero)
	local windowSize = Value(Vector2.zero)
	local absSize = Value(Vector2.zero)

	local scrollFrame = Value(nil)
	local function computeShowBar()
		local scrollFrame = unwrap(scrollFrame)
		if scrollFrame==nil then
			barVisibility.Vertical:set(false)
			barVisibility.Horizontal:set(false)
			return
		end
		
		--apparently there's decimals included with these sizes
		--so we need to round the sizes to the nearest pixel so
		--the scrollbar padding/inset matches what is visually shown
		local windowSize = scrollFrame.AbsoluteWindowSize
		local canvasSize = scrollFrame.AbsoluteCanvasSize
		barVisibility.Vertical:set(math.round(windowSize.Y) < math.round(canvasSize.Y))
		barVisibility.Horizontal:set(math.round(windowSize.X) < math.round(canvasSize.X))
	end

	local zIndex = props.ZIndex or 1
	local childZIndex = Computed(function()
		return unwrap(zIndex) + 10
	end)

	local containerFrame = Background {
		Name = "BaseScrollFrame",

		[Children] = {
			ScrollBar {
				ZIndex = childZIndex,
				
				IsVertical = true,
				BorderMode = props.ScrollBarBorderMode,
				BarVisibility = barVisibility,
				VerticalScrollBarPosition = vertPos,
				CanvasPosition = canvasPosition,
				AbsoluteCanvasSize = absCanvasSize,
				AbsoluteSize = absSize,
				WindowSize = windowSize,
				ScrollBarThickness = scrollBarThickness,
			},
			ScrollBar {
				ZIndex = childZIndex,
				
				IsVertical = false,
				BorderMode = props.ScrollBarBorderMode,
				BarVisibility = barVisibility,
				VerticalScrollBarPosition = vertPos,
				CanvasPosition = canvasPosition,
				AbsoluteCanvasSize = absCanvasSize,
				AbsoluteSize = absSize,
				WindowSize = windowSize,
				ScrollBarThickness = scrollBarThickness,
			},
			New "ScrollingFrame" {
				[Ref] = scrollFrame,

				Name = "Canvas",
				Size = UDim2.fromScale(1, 1),
				BottomImage = "rbxasset://textures/ui/Scroll/scroll-middle.png",
				TopImage = "rbxasset://textures/ui/Scroll/scroll-middle.png",
				MidImage = "rbxasset://textures/ui/Scroll/scroll-middle.png",
				ScrollingEnabled = isEnabled,
				ScrollBarThickness = props.ScrollBarThickness or DEFAULT_SCROLL_BAR_THICKNESS,
				VerticalScrollBarPosition = vertPos,
				HorizontalScrollBarInset = Enum.ScrollBarInset.ScrollBar,
				VerticalScrollBarInset = vertInset,
				ScrollBarImageTransparency = 1,
				BackgroundTransparency = 1,
				BorderSizePixel = 0,
				
				ZIndex = zIndex,
				
				CanvasSize = props.CanvasSize,
				AutomaticCanvasSize = props.AutomaticCanvasSize,

				CanvasPosition = canvasPosition,
				[Out "CanvasPosition"] = canvasPosition,
				[Out "AbsoluteCanvasSize"] = absCanvasSize,
				[Out "AbsoluteWindowSize"] = windowSize,
				[Out "AbsoluteSize"] = absSize,
				[Out "ScrollBarThickness"] = scrollBarThickness,
				
				[OnChange "AbsoluteWindowSize"] = computeShowBar,
				[OnChange "AbsoluteCanvasSize"] = computeShowBar,

				[Children] = {
					props[Children],
				}
			}
		}
	}

	local hydrateProps = stripProps(props, COMPONENT_ONLY_PROPERTIES)
	return Hydrate(containerFrame)(hydrateProps)
end