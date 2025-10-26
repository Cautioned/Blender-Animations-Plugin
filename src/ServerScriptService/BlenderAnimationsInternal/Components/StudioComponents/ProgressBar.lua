-- Written by @boatbomber

local Plugin = script:FindFirstAncestorWhichIsA("Plugin")
local Fusion = require(Plugin:FindFirstChild("Fusion", true))

local StudioComponents = script.Parent
local StudioComponentsUtil = StudioComponents:FindFirstChild("Util")

local getMotionState = require(StudioComponentsUtil.getMotionState)
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
	local frame = New "Frame" {
		Name = "Loading",
		BackgroundColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.ScrollBarBackground),
		Size = UDim2.new(0,constants.TextSize*6, 0, constants.TextSize),
		ClipsDescendants = true,

		[Children] = {
			New "UICorner" {
				CornerRadius = constants.CornerRadius,
			},
			New "Frame" {
				Name = "Fill",
				BackgroundColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.DialogMainButton),
				
				Size = getMotionState(Computed(function()
					return UDim2.fromScale(unwrap(props.Progress), 1)
				end), "Spring", 40),

				[Children] = New "UICorner" {
					CornerRadius = constants.CornerRadius,
				}
			},
		}
	}

    local hydrateProps = stripProps(props, COMPONENT_ONLY_PROPERTIES)
    return Hydrate(frame)(hydrateProps)
end