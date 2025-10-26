-- Written by @boatbomber

local Plugin = script:FindFirstAncestorWhichIsA("Plugin")
local Fusion = require(Plugin:FindFirstChild("Fusion", true))

local StudioComponents = script.Parent
local StudioComponentsUtil = StudioComponents:FindFirstChild("Util")

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
}

type LoadingProperties = {
	Enabled: types.CanBeState<boolean>?,
	[any]: any,
}

local cos = math.cos
local clock = os.clock
local pi4 = 12.566370614359172 --4*pi

return function(props: LoadingProperties): Frame
	local isEnabled = getState(props.Enabled, true)
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
		BackgroundTransparency = 1,
		Size = UDim2.new(0,constants.TextSize*4, 0,constants.TextSize*1.5),
		Visible = isEnabled,
		ClipsDescendants = true,
		[Cleanup] = haltAnim,

		[Children] = {
			New "Frame" {
				Name = "Bar1",
				BackgroundColor3 = colorA,
				Size = sizeA,
				Position = UDim2.fromScale(0.02, 0.5),
				AnchorPoint = Vector2.new(0,0.5),
			},
			New "Frame" {
				Name = "Bar2",
				BackgroundColor3 = colorB,
				Size = sizeB,
				Position = UDim2.fromScale(0.5, 0.5),
				AnchorPoint = Vector2.new(0.5,0.5),
			},
			New "Frame" {
				Name = "Bar3",
				BackgroundColor3 = colorA,
				Size = sizeA,
				Position = UDim2.fromScale(0.98, 0.5),
				AnchorPoint = Vector2.new(1,0.5),
			},
		}
	}

	local hydrateProps = stripProps(props, COMPONENT_ONLY_PROPERTIES)
	return Hydrate(frame)(hydrateProps)
end
