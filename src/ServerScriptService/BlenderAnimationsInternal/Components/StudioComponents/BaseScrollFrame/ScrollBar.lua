-- Writen by @boatbomber
-- Modified by @mvyasu

local Plugin = script:FindFirstAncestorWhichIsA("Plugin")
local Fusion = require(Plugin:FindFirstChild("Fusion", true))

local StudioComponents = script.Parent.Parent
local StudioComponentsUtil = StudioComponents:FindFirstChild("Util")

local ScrollArrow = require(script.Parent.ScrollArrow)

local themeProvider = require(StudioComponentsUtil.themeProvider)
local getModifier = require(StudioComponentsUtil.getModifier)
local stripProps = require(StudioComponentsUtil.stripProps)
local unwrap = require(StudioComponentsUtil.unwrap)
local types = require(StudioComponentsUtil.types)

local Computed = Fusion.Computed
local Children = Fusion.Children
local OnEvent = Fusion.OnEvent
local Hydrate = Fusion.Hydrate
local Value = Fusion.Value
local New = Fusion.New

local COMPONENT_ONLY_PROPERTIES = {
	"VerticalScrollBarPosition",
	"ScrollBarThickness",
	"CanvasPosition",
	"AbsoluteCanvasSize",
	"BarVisibility",
	"AbsoluteSize",
	"WindowSize",
	"IsVertical",
	"BorderMode",
}

type ScrollBarProperties = {
	VerticalScrollBarPosition: types.CanBeState<Enum.VerticalScrollBarPosition>?,
	BorderMode: types.CanBeState<Enum.BorderMode>?,
	CanvasPosition: types.CanBeState<UDim2>,
	AbsoluteSize: types.CanBeState<Vector2>,
	AbsoluteCanvasSize: types.CanBeState<Vector2>,
	WindowSize: types.CanBeState<Vector2>,
	ScrollBarThickness: types.CanBeState<number>,
	BarVisibility: {
		Horizontal: types.CanBeState<boolean>,
		Vertical: types.CanBeState<boolean>,
	},
	IsVertical: boolean,
	[any]: any,
}

return function(props: ScrollBarProperties): ImageButton	
	local scrollBarThickness = props.ScrollBarThickness
	local absoluteCanvasSize = props.AbsoluteCanvasSize
	local canvasPosition = props.CanvasPosition
	local windowSize = props.WindowSize
	
	local isHoveringHandle = Value(false)
	local isPressingHandle = Value(false)
	
	local frameBorderMode = props.BorderMode or Enum.BorderMode.Inset
	local childborderMode = Enum.BorderMode.Outline
	local borderSize = 1
	
	local scrollBarOffset = Computed(function()
		local allVisible = true
		for _,visibleState in pairs(props.BarVisibility) do
			if not unwrap(visibleState) then
				allVisible = false
				break
			end
		end
		local isInsetBorder = unwrap(frameBorderMode)==Enum.BorderMode.Inset
		return if allVisible then -(unwrap(scrollBarThickness) or 0) + (if isInsetBorder then borderSize else 0) else 0
	end)

	local scrollBarHandleOffset = Computed(function()
		local offsetSize = unwrap(scrollBarOffset)
		local size = (unwrap(props.AbsoluteSize) or Vector2.zero) + (Vector2.one * (unwrap(scrollBarOffset) or Vector2.zero))
		local scrollbarThickness = unwrap(scrollBarThickness) or 0
		local isInsetBorder = unwrap(frameBorderMode)==Enum.BorderMode.Inset
		return (2*scrollbarThickness+(if isInsetBorder then -borderSize*2 else 0))/(if props.IsVertical then size.Y else size.X)
	end)
	
	return Hydrate(New "Frame" {
		Name = (if props.IsVertical then "Vertical" else "Horizontal").."ScrollBar",
		BorderMode = frameBorderMode,
		BorderSizePixel = borderSize,
		
		Visible = Computed(function()
			return unwrap(if props.IsVertical then props.BarVisibility.Vertical else props.BarVisibility.Horizontal)
		end),
		
		BackgroundColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.ScrollBarBackground),
		BorderColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.Border),
		
		AnchorPoint = if props.IsVertical then Computed(function()
			local vert = unwrap(props.VerticalScrollBarPosition)
			if vert == Enum.VerticalScrollBarPosition.Right then
				return Vector2.new(1, 0)
				else
				return Vector2.new(0, 0)
			end
		end) else Vector2.new(0, 1),
		
		Position = if props.IsVertical then Computed(function()
			local vert = unwrap(props.VerticalScrollBarPosition)
			if vert == Enum.VerticalScrollBarPosition.Right then
				return UDim2.fromScale(1, 0)
				else
				return UDim2.fromScale(0, 0)
			end
		end) else Computed(function()
			local vert = unwrap(props.VerticalScrollBarPosition)
			return UDim2.fromScale(if vert==Enum.VerticalScrollBarPosition.Left then 1 else 0, 1)
		end),
		
		Size = if props.IsVertical then Computed(function()
			return UDim2.new(0, unwrap(scrollBarThickness), 1, unwrap(scrollBarOffset))
		end) else Computed(function()
			local scrollBarThickness = unwrap(scrollBarThickness)
			return  UDim2.new(1, unwrap(scrollBarOffset), 0, scrollBarThickness)
		end),

		[Children] = {
			(function()
				local scrollArrows = {}
				for _,direction in pairs(if props.IsVertical then {"Up", "Down"} else {"Left", "Right"}) do
					table.insert(scrollArrows, ScrollArrow {
						Name = direction.."Arrow",
						BorderMode = childborderMode,
						BorderSizePixel = borderSize,
						ZIndex = props.ZIndex,
						Direction = direction,
						Size = UDim2.fromScale(1, 1),
						SizeConstraint = if props.IsVertical then Enum.SizeConstraint.RelativeXX else Enum.SizeConstraint.RelativeYY,
						Activated = (function()
							if direction=="Up" or direction=="Left" then
								return function()
									local p = unwrap(canvasPosition) or Vector2.zero
									canvasPosition:set(Vector2.new(
										if props.IsVertical then p.X else math.clamp(p.X-25, 0, math.huge),
										if props.IsVertical then math.clamp(p.Y-25, 0, math.huge) else p.Y
										))
								end
							elseif direction=="Down" or direction=="Right" then
								return function()
									local p = unwrap(canvasPosition) or Vector2.zero
									local window = unwrap(windowSize) or Vector2.zero
									local content = unwrap(absoluteCanvasSize) or Vector2.zero
									canvasPosition:set(Vector2.new(
										if props.IsVertical then p.X else math.clamp(p.X+25, 0, content.X-window.X),
										if props.IsVertical then math.clamp(p.Y+25, 0, content.Y-window.Y) else p.Y
										))
								end
							end
						end)(),
					})
				end
				return scrollArrows
			end)(),
			
			
			New "Frame" {
				Name = "Handle",
				ZIndex = props.ZIndex,
				BorderMode = childborderMode,
				BorderSizePixel = borderSize,
			
				BorderColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.Border),
				
				BackgroundColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.ScrollBar, getModifier({
					Enabled = true,
					Pressed = Computed(function()
						return unwrap(isPressingHandle) or unwrap(isHoveringHandle)
					end),
				})),
			
				Size = Computed(function()
					local window = unwrap(windowSize) or Vector2.zero
					local content = unwrap(absoluteCanvasSize) or Vector2.zero
					local relativeOffset = unwrap(scrollBarHandleOffset)
				
					return UDim2.fromScale(
						if props.IsVertical then 1 else (window.X/content.X) * (1-relativeOffset),
						if props.IsVertical then (window.Y/content.Y) * (1-relativeOffset) else 1
					)
				end),
			
				Position = Computed(function()
					local content = unwrap(absoluteCanvasSize) or Vector2.zero
					local pos = unwrap(canvasPosition) or Vector2.zero
					local scrollBarThickness = unwrap(scrollBarThickness) or 0
				
					local relativeOffset = unwrap(scrollBarHandleOffset) or 0
					local relativePos = if props.IsVertical then (pos.Y/content.Y) else (pos.X/content.X)
				
					if props.IsVertical then
						return UDim2.new(
							0, 0,
							relativePos * (1-relativeOffset), unwrap(scrollBarThickness)-borderSize
						)
					else
						return UDim2.new(
							relativePos * (1-relativeOffset), unwrap(scrollBarThickness)-borderSize,
							0, 0
						)
					end
				end),
			
				[OnEvent "InputBegan"] = function(inputObject)
					if inputObject.UserInputType == Enum.UserInputType.MouseMovement then
						isHoveringHandle:set(true)
					elseif inputObject.UserInputType == Enum.UserInputType.MouseButton1 then
						isPressingHandle:set(true)
					end
				end,

				[OnEvent "InputEnded"] = function(inputObject)
					if inputObject.UserInputType == Enum.UserInputType.MouseMovement then
						isHoveringHandle:set(false)
					elseif inputObject.UserInputType == Enum.UserInputType.MouseButton1 then
						isPressingHandle:set(false)
					end
				end,
			},
		}
	})(stripProps(props, COMPONENT_ONLY_PROPERTIES))
end