local Plugin = script:FindFirstAncestorWhichIsA("Plugin")
local Fusion = require(Plugin:FindFirstChild("Fusion", true))

local StudioComponents = script.Parent
local StudioComponentsUtil = StudioComponents:FindFirstChild("Util")

local themeProvider = require(StudioComponentsUtil.themeProvider)
local constants = require(StudioComponentsUtil.constants)
local unwrap = require(StudioComponentsUtil.unwrap)
local types = require(StudioComponentsUtil.types)

local New = Fusion.New
local Value = Fusion.Value
local Computed = Fusion.Computed
local Children = Fusion.Children
local OnChange = Fusion.OnChange
local Hydrate = Fusion.Hydrate

type BoxBorderProperties = {
	Color: types.CanBeState<Color3>?,
	Thickness: types.CanBeState<number>?,
	CornerRadius: types.CanBeState<UDim>?,
	[types.Children]: GuiObject,
}

-- using [Children] to define the GuiObject is meant to give a more consistant format

return function(props: BoxBorderProperties): GuiObject
	local boxProps = props or {}
	local borderColor = boxProps.Color or themeProvider:GetColor(Enum.StudioStyleGuideColor.Border)
	
	local hydrateProps = {
		BorderColor3 = borderColor,
		BorderMode = Enum.BorderMode.Inset,
		BorderSizePixel = Computed(function()
			local thickness = unwrap(boxProps.Thickness)
			local useCurvedBoxes = unwrap(constants.CurvedBoxes)
			return if useCurvedBoxes then 0 else (thickness or 1)
		end),
	}
	
	if unwrap(constants.CurvedBoxes) then
		local backgroundTransparency = Value(props[Children].BackgroundTransparency)
		
		hydrateProps = {
			[Children] = {
				New "UICorner" {
					CornerRadius = boxProps.CornerRadius or constants.CornerRadius
				},
				
				New "UIStroke" {
					ApplyStrokeMode = Enum.ApplyStrokeMode.Border,
					Color = borderColor,
					Thickness = boxProps.Thickness or 1,
					Transparency = backgroundTransparency,
				}
			},
			
			[OnChange "BackgroundTransparency"] = function(newTransparency)
				backgroundTransparency:set(newTransparency)
			end,
		}
	end
	
	return Hydrate(props[Children])(hydrateProps)
end
