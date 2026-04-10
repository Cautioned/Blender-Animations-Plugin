-- Written by @boatbomber

local Plugin = script:FindFirstAncestorWhichIsA("Plugin")
local Fusion = require(Plugin:FindFirstChild("Fusion", true))

local StudioComponents = script.Parent
local StudioComponentsUtil = StudioComponents:FindFirstChild("Util")
local BoxBorder = require(StudioComponents.BoxBorder)

local themeProvider = require(StudioComponentsUtil.themeProvider)
local stripProps = require(StudioComponentsUtil.stripProps)
local constants = require(StudioComponentsUtil.constants)
local unwrap = require(StudioComponentsUtil.unwrap)
local types = require(StudioComponentsUtil.types)

local Computed = Fusion.Computed
local Children = Fusion.Children
local Hydrate = Fusion.Hydrate
local New = Fusion.New

local COMPONENT_ONLY_PROPERTIES = {
	"Progress",
}

type ProgressProperties = {
	Progress: (number | types.StateObject<number>)?,
	[any]: any,
}

return function(props: ProgressProperties): Frame
	local progress = Computed(function()
		return math.max(0, math.min(1, unwrap(props.Progress) or 0))
	end)
	local zIndex = props.ZIndex or 1

	local frame = BoxBorder {
		Color = themeProvider:GetColor(Enum.StudioStyleGuideColor.ButtonBorder),
		[Children] = New "Frame" {
			Name = "Loading",
			BackgroundColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.ScrollBarBackground),
			Size = UDim2.new(0,constants.TextSize*6, 0, constants.TextSize),
			ClipsDescendants = true,
			ZIndex = zIndex,

			[Children] = {
				New "UICorner" {
					CornerRadius = constants.CornerRadius,
				},
				New "Frame" {
					Name = "Fill",
					BackgroundColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.DialogMainButton),
					BackgroundTransparency = 0,
					Position = UDim2.fromOffset(0, 0),
					BorderSizePixel = 0,
					Visible = Computed(function()
						return progress:get() > 0
					end),
					ZIndex = zIndex,
					
					Size = Computed(function()
						local currentProgress = progress:get()
						if currentProgress <= 0 then
							return UDim2.fromOffset(0, 0)
						end

						return UDim2.new(currentProgress, math.max(2, constants.TextSize * 0.35), 1, 0)
					end),

					[Children] = New "UICorner" {
						CornerRadius = constants.CornerRadius,
					}
				}
			},
		}
	}

    local hydrateProps = stripProps(props, COMPONENT_ONLY_PROPERTIES)
    return Hydrate(frame)(hydrateProps)
end