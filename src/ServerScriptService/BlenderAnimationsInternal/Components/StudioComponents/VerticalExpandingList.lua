-- Roact version by @sircfenner
-- Ported to Fusion by @YasuYoshida

local Plugin = script:FindFirstAncestorWhichIsA("Plugin")
local Fusion = require(Plugin:FindFirstChild("Fusion", true))

local StudioComponents = script.Parent
local StudioComponentsUtil = StudioComponents:FindFirstChild("Util")

local Background = require(StudioComponents.Background)
local BoxBorder = require(StudioComponents.BoxBorder)

local getMotionState = require(StudioComponentsUtil.getMotionState)
local stripProps = require(StudioComponentsUtil.stripProps)
local unwrap = require(StudioComponentsUtil.unwrap)
local types = require(StudioComponentsUtil.types)

local Children = Fusion.Children
local Computed = Fusion.Computed
local Hydrate = Fusion.Hydrate
local New = Fusion.New
local Value = Fusion.Value
local Out = Fusion.Out

local COMPONENT_ONLY_PROPERTIES = {
	"Padding",
	"AutomaticSize",
}

type VerticalExpandingListProperties = {
	Padding: (UDim | types.StateObject<UDim>)?,
	[any]: any,
}

return function(props: VerticalExpandingListProperties): Frame
	local hydrateProps = stripProps(props, COMPONENT_ONLY_PROPERTIES)

	local contentSize = Value(Vector2.new(0,0))

	return Hydrate(
		BoxBorder {
			[Children] = Background {
				ClipsDescendants = true,
				Size = getMotionState(Computed(function()
					local mode = unwrap(props.AutomaticSize or Enum.AutomaticSize.Y) -- Custom autosize since engine sizing is unreliable
					if mode == Enum.AutomaticSize.Y then
						local s = unwrap(contentSize)
						if s then
							return UDim2.new(1,0,0,s.Y)
						else
							return UDim2.new(1,0,0,0)
						end
					else
						return props.Size or UDim2.new(1,0,0,0)
					end
				end), "Spring", 40),

				[Children] = New "UIListLayout" {
					SortOrder = Enum.SortOrder.LayoutOrder,
					FillDirection = Enum.FillDirection.Vertical,

					Padding = Computed(function()
						return unwrap(props.Padding) or UDim.new(0, 10)
					end),

					[Out "AbsoluteContentSize"] = contentSize,
				}
			}
		}
	)(hydrateProps)
end