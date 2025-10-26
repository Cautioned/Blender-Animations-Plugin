-- Roact version by @sircfenner
-- Ported to Fusion by @YasuYoshida

local Plugin = script:FindFirstAncestorWhichIsA("Plugin")
local Fusion = require(Plugin:FindFirstChild("Fusion", true))

local StudioComponents = script.Parent.Parent
local StudioComponentsUtil = StudioComponents:FindFirstChild("Util")

local themeProvider = require(StudioComponentsUtil.themeProvider)
local getModifier = require(StudioComponentsUtil.getModifier)
local stripProps = require(StudioComponentsUtil.stripProps)
local getState = require(StudioComponentsUtil.getState)
local unwrap = require(StudioComponentsUtil.unwrap)
local types = require(StudioComponentsUtil.types)

local Computed = Fusion.Computed
local OnEvent = Fusion.OnEvent
local Hydrate = Fusion.Hydrate
local Cleanup = Fusion.Cleanup
local Value = Fusion.Value
local New = Fusion.New

local DEFAULT_ARROW_SIZE = UDim2.fromOffset(12, 12)
local DEFAULT_ARROW_IMAGE = "rbxassetid://6677623152"
local DEFAULT_ARROW_RECT_SIZE = 16

local COMPONENT_ONLY_PROPERTIES = {
	"Enabled",
	"Activated",
	"Direction",
	"ZIndex",
}

type ScrollArrowProperties = {
	Enabled: types.CanBeState<boolean>?,
	Direction: types.CanBeState<string>,
	Activated: () -> nil,
	[any]: any,
}

local function getBaseProperties(mainModifier: types.CanBeState<Enum.StudioStyleGuideModifier>, props: ScrollArrowProperties)
	return {
		Size = DEFAULT_ARROW_SIZE,
		Image = DEFAULT_ARROW_IMAGE,
		ImageRectSize = Vector2.new(DEFAULT_ARROW_RECT_SIZE, DEFAULT_ARROW_RECT_SIZE),
		ScaleType = Enum.ScaleType.Fit,
		
		ImageColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.TitlebarText, mainModifier),
		BackgroundColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.ScrollBar, mainModifier),
		BorderColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.Border, mainModifier),
		
		AnchorPoint = Computed(function()
			local currentDirection = unwrap(props.Direction)
			if currentDirection=="Down" then
				return Vector2.new(0, 1)
			elseif currentDirection=="Right" then
				return Vector2.new(1, 0)
			end
			return Vector2.new(0, 0)
		end),
		
		Position = Computed(function()
			local currentDirection = unwrap(props.Direction)
			if currentDirection=="Down" then
				return UDim2.fromScale(0, 1)
			elseif currentDirection=="Right" then
				return UDim2.fromScale(1, 0)
			end
			return UDim2.fromScale(0, 0)
		end),
		
		ImageRectOffset = Computed(function()
			local currentDirection = unwrap(props.Direction)
			if currentDirection=="Down" then
				return Vector2.new(0, DEFAULT_ARROW_RECT_SIZE)
			elseif currentDirection=="Left" then
				return Vector2.new(DEFAULT_ARROW_RECT_SIZE, 0)
			elseif currentDirection=="Right" then
				return Vector2.new(DEFAULT_ARROW_RECT_SIZE, DEFAULT_ARROW_RECT_SIZE)
			end
			return  Vector2.new(0, 0)
		end),
	}
end

return function(props: ScrollArrowProperties): ImageButton
	local isEnabled = getState(props.Enabled, true)
	local isHovering = Value(false)
	local isPressed = Value(false)
	
	-- no need for hover to be taken into account
	local arrowModifier = getModifier({
		Enabled = isEnabled,
		Pressed = isPressed,
	})
	
	local hearbeatConnection = nil
	local function disconnectHearbeat()	
		if hearbeatConnection then
			hearbeatConnection:Disconnect()
			hearbeatConnection = nil
		end
	end
	
	local function listenToHearbeat()
		if hearbeatConnection then
			return
		end
		if props.Activated then
			local nextAt = os.clock() + 0.35
			hearbeatConnection = game:GetService("RunService").Heartbeat:Connect(function()
				local now = os.clock()
				if now >= nextAt then
					if unwrap(isHovering, false) then
						props.Activated()
					end
					nextAt += 0.05
				end
			end)
		end
	end

	local zIndex = Computed(function()
		return unwrap(props.ZIndex) or 2
	end)

	local newScrollArrow = New "ImageButton" {
		AutoButtonColor = false,
		ZIndex = zIndex,
		
		[Cleanup] = disconnectHearbeat,
		
		Active = Computed(function()
			local isEnabled = unwrap(isEnabled)
			if not isEnabled then
				disconnectHearbeat()
			end
			return isEnabled
		end),
		
		[OnEvent "InputBegan"] = function(inputObject)
			if not unwrap(isEnabled) then
				return
			elseif inputObject.UserInputType == Enum.UserInputType.MouseMovement then
				isHovering:set(true)
			elseif inputObject.UserInputType == Enum.UserInputType.MouseButton1 then
				isPressed:set(true)
				if props.Activated then
					props.Activated()
				end
				listenToHearbeat()
			end
		end,
		
		[OnEvent "InputEnded"] = function(inputObject)
			if not unwrap(isEnabled) then
				return
			elseif inputObject.UserInputType == Enum.UserInputType.MouseMovement then
				isHovering:set(false)
			elseif inputObject.UserInputType == Enum.UserInputType.MouseButton1 then
				isPressed:set(false)
				disconnectHearbeat()
			end
		end,
	}
	
	for index, value in pairs(getBaseProperties(arrowModifier, props)) do
		if props[index]==nil then
			props[index] = value
		end
	end

	return Hydrate(newScrollArrow)(stripProps(props, COMPONENT_ONLY_PROPERTIES))
end