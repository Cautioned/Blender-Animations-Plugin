-- Roact version by @sircfenner
-- Ported to Fusion by @YasuYoshida

local Plugin = script:FindFirstAncestorWhichIsA("Plugin")
local Fusion = require(Plugin:FindFirstChild("Fusion", true))

local StudioComponents = script.Parent
local StudioComponentsUtil = StudioComponents:FindFirstChild("Util")

local BoxBorder = require(StudioComponents.BoxBorder)

local themeProvider = require(StudioComponentsUtil.themeProvider)
local getDragInput = require(StudioComponentsUtil.getDragInput)
local stripProps = require(StudioComponentsUtil.stripProps)
local getState = require(StudioComponentsUtil.getState)
local unwrap = require(StudioComponentsUtil.unwrap)
local types = require(StudioComponentsUtil.types)

local New = Fusion.New
local Value = Fusion.Value
local Computed = Fusion.Computed
local Children = Fusion.Children
local OnEvent = Fusion.OnEvent
local Hydrate = Fusion.Hydrate
local Observer = Fusion.Observer
local Cleanup = Fusion.Cleanup
local Ref = Fusion.Ref

local COMPONENT_ONLY_PROPERTIES = {
	"ZIndex",
	"Enabled",
	"OnChange",
	"ListDisplayMode",
	"Value",
	"Step",
}

type ColorPickerProperties = {
	ListDisplayMode: (Enum.ListDisplayMode | types.StateObject<Enum.ListDisplayMode>)?,
	Enabled: (boolean | types.StateObject<boolean>)?,
	OnChange: (newColor: Color3) -> nil,
	Value: (Color3 | types.Value<Color3>)?,
	Step: (Vector2 | types.Value<Vector2>)?,
	[any]: any,
}

return function(props: ColorPickerProperties): Frame
	local listDisplayMode = getState(props.ListDisplayMode, Enum.ListDisplayMode.Horizontal)

	local isEnabled = getState(props.Enabled, true)
	local isHovering = Value(false)

	local isHorizontalList = Computed(function()
		return unwrap(listDisplayMode)==Enum.ListDisplayMode.Horizontal
	end)

	local regionRef = Value()
	local sliderRef = Value()

	local currentRegionInput = getDragInput({
		Enabled = isEnabled,
		Instance = regionRef,
		Value = Value(Vector2.new()),
	})

	local currentSliderInput = getDragInput({
		Enabled = isEnabled,
		Instance = sliderRef,
		Value = Value(Vector2.new()),
	})

	local inputColor = getState(props.Value, Color3.new(1, 1, 1))
	local function updateCurrentInput()
		local hue, sat, val = unwrap(inputColor, false):ToHSV()
		currentRegionInput:set(Vector2.new(1-hue, 1-sat))
		currentSliderInput:set(if unwrap(isHorizontalList, false) then Vector2.new(0, 1-val) else Vector2.new(val, 0))
	end

	updateCurrentInput()

	local currentColor = Computed(function()
		local regionInput = unwrap(currentRegionInput)
		local sliderInput = unwrap(currentSliderInput)
		return Color3.fromHSV(
			math.max(0.0001, 1 - regionInput.X),
			math.max(0.0001, 1 - regionInput.Y),
			math.max(0.0001, 1 - if unwrap(isHorizontalList, false) then sliderInput.Y else 1-sliderInput.X)
		)
	end)

	local function roundNumber(number: number)
		return if (1-number)<.01 or number<.01 then math.round(number) else number
	end

	local lastUpdatedColor = nil
	local cleanupInputColorObserver = Observer(inputColor):onChange(updateCurrentInput)
	local cleanupCurrentColorObserver = Observer(currentColor):onChange(function()
		local newColor = unwrap(currentColor, false)
		if props.OnChange then
			local roundedColor = Color3.new(
				roundNumber(newColor.R),
				roundNumber(newColor.G),
				roundNumber(newColor.B)
			)

			if lastUpdatedColor~=roundedColor then
				lastUpdatedColor = roundedColor
				-- to prevent dependency issues
				task.spawn(function()
					-- due to the math.max earlier, I need to round to the nearest whole number just in case
					props.OnChange(roundedColor)
				end)
			end
		end
	end)

	local childZIndex = Computed(function()
		return (unwrap(props.ZIndex) or 0) + 1
	end)

	local newColorPicker = New "Frame" {
		Name = "ColorPicker",
		Size = UDim2.new(1, 0, 0, 150),
		BackgroundTransparency = 1,
		[Cleanup] = function()
			cleanupInputColorObserver()
			cleanupCurrentColorObserver()
		end,

		[Children] = {
			BoxBorder {
				[Children] = New "TextButton" {
					Name = "Slider",
					ZIndex = childZIndex,
					Active = false,
					AutoButtonColor = false,
					Text = "",
					Size = Computed(function()
						if unwrap( isHorizontalList) then
							return UDim2.new(0, 14, 1, 0)
						end
						return UDim2.new(1, 0, 0, 14)
					end),
					AnchorPoint = Computed(function()
						if unwrap( isHorizontalList) then
							return Vector2.new(1, 0)
						end
						return Vector2.new(0, 1)
					end),
					Position = Computed(function()
						if unwrap( isHorizontalList) then
							return UDim2.new(1, -6, 0, 0)
						end
						return UDim2.new(0, 0, 1, -6)
					end),
					BorderSizePixel = 0,
					BackgroundColor3 = Color3.fromRGB(255, 255, 255),

					[Ref] = sliderRef,

					[Children] = {
						New "UIGradient" {
							Name = "Gradient",
							Color = Computed(function()
								local isEnabled = unwrap(isEnabled)
								local hue, sat, val = unwrap(currentColor):ToHSV()
								return ColorSequence.new(Color3.fromRGB(0, 0, 0), Color3.fromHSV(hue, sat, if isEnabled then 1 else .5))
							end),
							Rotation = Computed(function()
								if unwrap( isHorizontalList) then
									return -90
								end
								return 0
							end),
						},
						New "ImageLabel" {
							Name = "Arrow",
							ZIndex = childZIndex,
							AnchorPoint = Computed(function()
								if unwrap( isHorizontalList) then
									return Vector2.new(0, .5)
								end
								return Vector2.new(.5, 0)
							end),
							Size = UDim2.fromOffset(5, 9),
							Rotation = Computed(function()
								if unwrap( isHorizontalList) then
									return 0
								end
								return 90
							end),
							Position = Computed(function()
								local scale = 1 - select(3, unwrap(currentColor):ToHSV())
								if unwrap( isHorizontalList) then
									return UDim2.new(1, 1, scale, 0)
								end
								return UDim2.new(1-scale, 0, 1, 1)
							end),
							BackgroundTransparency = 1,
							Image = "rbxassetid://7507468017",
							ImageColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.TitlebarText),
						}
					}
				}
			},
			BoxBorder {
				[Children] = New "ImageButton" {
					Name = "Region",
					ZIndex = childZIndex,
					Active = false,
					AutoButtonColor = false,
					Size = Computed(function()
						if unwrap( isHorizontalList) then
							return UDim2.new(1, -30, 1, 0)
						end
						return UDim2.new(1, 0, 1, -30)
					end),
					Image = "rbxassetid://2752294886",
					ImageColor3 = Computed(function()
						return Color3.fromHSV(0, 0, if unwrap(isEnabled) then 1 else .5)
					end),
					ClipsDescendants = true,
					BorderSizePixel = 0,
					[Ref] = regionRef,

					[Children] = New "Frame" {
						Name = "Indicator",
						ZIndex = childZIndex,
						AnchorPoint = Vector2.new(0.5, 0.5),
						Position = Computed(function()
							local hue, sat, val = unwrap(currentColor):ToHSV()
							return UDim2.new(1 - hue, 1, 1 - sat, 0)
						end),
						Size = UDim2.fromOffset(19, 19),
						BackgroundTransparency = 1,

						[Children] = {
							New "Frame" {
								Name = "Vertical",
								ZIndex = childZIndex,
								Position = UDim2.fromOffset(8, 0),
								Size = UDim2.new(0, 2, 1, 0),
								BorderSizePixel = 0,
								BackgroundColor3 = Color3.fromRGB(0, 0, 0),
							},
							New "Frame" {
								Name = "Horizontal",
								ZIndex = childZIndex,
								Position = UDim2.fromOffset(0, 8),
								Size = UDim2.new(1, 0, 0, 2),
								BorderSizePixel = 0,
								BackgroundColor3 = Color3.fromRGB(0, 0, 0),
							}
						}
					}
				}
			},
		}
	}

	local hydrateProps = stripProps(props, COMPONENT_ONLY_PROPERTIES)
	return Hydrate(newColorPicker)(hydrateProps)
end