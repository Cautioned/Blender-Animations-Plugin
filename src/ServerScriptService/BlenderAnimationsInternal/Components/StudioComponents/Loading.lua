-- Written by @boatbomber

local Plugin = script:FindFirstAncestorWhichIsA("Plugin")
local Fusion = require(Plugin:FindFirstChild("Fusion", true))

local StudioComponents = script.Parent
local StudioComponentsUtil = StudioComponents:FindFirstChild("Util")
local ProgressBar = require(StudioComponents.ProgressBar)

local getMotionState = require(StudioComponentsUtil.getMotionState)
local themeProvider = require(StudioComponentsUtil.themeProvider)
local stripProps = require(StudioComponentsUtil.stripProps)
local constants = require(StudioComponentsUtil.constants)
local getState = require(StudioComponentsUtil.getState)
local unwrap = require(StudioComponentsUtil.unwrap)
local types = require(StudioComponentsUtil.types)

local Computed = Fusion.Computed
local Hydrate = Fusion.Hydrate
local Value = Fusion.Value
local New = Fusion.New
local Observer = Fusion.Observer
local Children = Fusion.Children
local Cleanup = Fusion.Cleanup

local COMPONENT_ONLY_PROPERTIES = {
	"Enabled",
	"Title",
	"Status",
	"Detail",
	"Progress",
	"CanEstimate",
}

type LoadingProperties = {
	Enabled: types.CanBeState<boolean>?,
	Title: types.CanBeState<string>?,
	Status: types.CanBeState<string>?,
	Detail: types.CanBeState<string>?,
	Progress: types.CanBeState<number>?,
	CanEstimate: types.CanBeState<boolean>?,
	[any]: any,
}

local cos = math.cos
local clock = os.clock
local pi4 = 12.566370614359172 --4*pi

return function(props: LoadingProperties): Frame
	local isEnabled = getState(props.Enabled, true)
	local title = getState(props.Title, "Working")
	local status = getState(props.Status, "Please wait...")
	local detail = getState(props.Detail, "")
	local progress = getState(props.Progress, 0)
	local canEstimate = getState(props.CanEstimate, false)
	local time = Value(0)

	local animThread = nil

	local function startMotion()
		if not unwrap(isEnabled) then return end

		if animThread then
			task.cancel(animThread)
			animThread = nil
		end

		animThread = task.defer(function()
			local startTime = clock()
			while unwrap(isEnabled) do
				time:set(clock()-startTime)
				task.wait(1/25) -- Springs will smooth out the motion so we needn't bother with high refresh rate here
			end
		end)
	end

	startMotion()
	local observeDisconnect = Observer(isEnabled):onChange(startMotion)

	local function haltAnim()
		observeDisconnect()
		if animThread then
			task.cancel(animThread)
			animThread = nil
		end
	end

	local light = themeProvider:GetColor(Enum.StudioStyleGuideColor.Light, Enum.StudioStyleGuideModifier.Default)
	local accent = themeProvider:GetColor(Enum.StudioStyleGuideColor.DialogMainButton, Enum.StudioStyleGuideModifier.Default)

	local alphaA = Computed(function()
		local t = (unwrap(time) + 0.25) * pi4
		return (cos(t)+1)/2
	end)
	local alphaB = Computed(function()
		local t = unwrap(time) * pi4
		return (cos(t)+1)/2
	end)

	local colorA = getMotionState(Computed(function()
		return unwrap(light):Lerp(unwrap(accent), unwrap(alphaA))
	end), "Spring", 40)

	local colorB = getMotionState(Computed(function()
		return unwrap(light):Lerp(unwrap(accent), unwrap(alphaB))
	end), "Spring", 40)

	local progressText = Computed(function()
		return string.format("%d%%", math.floor(math.clamp(unwrap(progress), 0, 1) * 100 + 0.5))
	end)

	local sizeA = getMotionState(Computed(function()
		local alpha = unwrap(alphaA)
		return UDim2.fromScale(
			0.2,
			0.5 + alpha*0.5
		)
	end), "Spring", 40)

	local sizeB = getMotionState(Computed(function()
		local alpha = unwrap(alphaB)
		return UDim2.fromScale(
			0.2,
			0.5 + alpha*0.5
		)
	end), "Spring", 40)

	local frame = New "Frame" {
		Name = "Loading",
		Active = isEnabled,
		BackgroundColor3 = Color3.new(0, 0, 0),
		BackgroundTransparency = 0.35,
		Size = UDim2.fromScale(1, 1),
		Visible = isEnabled,
		ZIndex = 20,
		[Cleanup] = haltAnim,

		[Children] = {
			New "TextButton" {
				Name = "InputBlocker",
				AutoButtonColor = false,
				Modal = true,
				Text = "",
				BackgroundTransparency = 1,
				Size = UDim2.fromScale(1, 1),
				ZIndex = 20,
			},
			New "Frame" {
				Name = "Panel",
				AnchorPoint = Vector2.new(0.5, 0.5),
				BackgroundColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.MainBackground),
				Position = UDim2.fromScale(0.5, 0.5),
				Size = UDim2.new(1, -28, 0, 170),
				ZIndex = 21,
				[Children] = {
					New "UICorner" {
						CornerRadius = constants.CornerRadius,
					},
					New "UIPadding" {
						PaddingBottom = UDim.new(0, 14),
						PaddingLeft = UDim.new(0, 14),
						PaddingRight = UDim.new(0, 14),
						PaddingTop = UDim.new(0, 14),
					},
					New "UIListLayout" {
						Padding = UDim.new(0, 8),
						SortOrder = Enum.SortOrder.LayoutOrder,
					},
					New "TextLabel" {
						AutomaticSize = Enum.AutomaticSize.Y,
						BackgroundTransparency = 1,
						Font = themeProvider:GetFont("Bold"),
						LayoutOrder = 1,
						Size = UDim2.new(1, 0, 0, 24),
						Text = title,
						TextColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.MainText),
						TextSize = constants.TextSize + 2,
						TextWrapped = true,
						TextXAlignment = Enum.TextXAlignment.Left,
						TextYAlignment = Enum.TextYAlignment.Top,
						ZIndex = 21,
					},
					New "TextLabel" {
						AutomaticSize = Enum.AutomaticSize.Y,
						BackgroundTransparency = 1,
						Font = themeProvider:GetFont("Default"),
						LayoutOrder = 2,
						Size = UDim2.new(1, 0, 0, 20),
						Text = status,
						TextColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.BrightText),
						TextSize = constants.TextSize,
						TextWrapped = true,
						TextXAlignment = Enum.TextXAlignment.Left,
						TextYAlignment = Enum.TextYAlignment.Top,
						ZIndex = 21,
					},
					New "TextLabel" {
						AutomaticSize = Enum.AutomaticSize.Y,
						BackgroundTransparency = 1,
						Font = themeProvider:GetFont("Default"),
						LayoutOrder = 3,
						Size = UDim2.new(1, 0, 0, 18),
						Text = detail,
						TextColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.DimmedText),
						TextSize = constants.TextSize - 1,
						TextWrapped = true,
						TextXAlignment = Enum.TextXAlignment.Left,
						TextYAlignment = Enum.TextYAlignment.Top,
						Visible = Computed(function()
							return unwrap(detail) ~= ""
						end),
						ZIndex = 21,
					},
					New "Frame" {
						AutomaticSize = Enum.AutomaticSize.Y,
						BackgroundTransparency = 1,
						LayoutOrder = 4,
						Size = UDim2.new(1, 0, 0, 48),
						Visible = canEstimate,
						ZIndex = 21,
						[Children] = {
							New "UIListLayout" {
								Padding = UDim.new(0, 6),
								SortOrder = Enum.SortOrder.LayoutOrder,
							},
							ProgressBar {
								LayoutOrder = 1,
								Progress = progress,
								Size = UDim2.new(1, 0, 0, 18),
								ZIndex = 21,
							},
							New "TextLabel" {
								BackgroundTransparency = 1,
								Font = themeProvider:GetFont("Mono"),
								LayoutOrder = 2,
								Size = UDim2.new(1, 0, 0, 16),
								Text = progressText,
								TextColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.MainText),
								TextSize = constants.TextSize - 1,
								TextXAlignment = Enum.TextXAlignment.Right,
								ZIndex = 21,
							},
						},
					},
					New "Frame" {
						BackgroundTransparency = 1,
						LayoutOrder = 5,
						Size = UDim2.new(1, 0, 0, constants.TextSize * 2),
						Visible = Computed(function()
							return not unwrap(canEstimate)
						end),
						ZIndex = 21,
						[Children] = {
							New "Frame" {
								Name = "Bar1",
								BackgroundColor3 = colorA,
								Size = sizeA,
								Position = UDim2.fromScale(0.02, 0.5),
								AnchorPoint = Vector2.new(0,0.5),
								ZIndex = 21,
							},
							New "Frame" {
								Name = "Bar2",
								BackgroundColor3 = colorB,
								Size = sizeB,
								Position = UDim2.fromScale(0.5, 0.5),
								AnchorPoint = Vector2.new(0.5,0.5),
								ZIndex = 21,
							},
							New "Frame" {
								Name = "Bar3",
								BackgroundColor3 = colorA,
								Size = sizeA,
								Position = UDim2.fromScale(0.98, 0.5),
								AnchorPoint = Vector2.new(1,0.5),
								ZIndex = 21,
							},
						},
					},
				},
			},
		}
	}

	local hydrateProps = stripProps(props, COMPONENT_ONLY_PROPERTIES)
	return Hydrate(frame)(hydrateProps)
end
