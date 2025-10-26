-- Roact version by @sircfenner
-- Ported to Fusion by @YasuYoshida

--[[

	1. onInputBegan
		setState({ Dragging = true })
		if widget then
			globalConnection = RunService.Heartbeat(() => onInput(widget.relativePosition))
		else
			globalConnection = InputService.InputChanged(() => onInput(input.Position))
	2. onInputEnded
		setState({ Dragging = false })
		globalConnection:Disconnect()
	3. onInputChanged
		if state.Dragging then
			onInput(input.Position)
	guiObject.InputBegan(onInputBegan)
	guiObject.InputEnded(onInputEnded)
	guiObject.InputChanged(onInputChanged)
	(3) is useful in the widget case because
	- heartbeat is a frame late
	- changed will fire on same frame at least while mouse is over the area
	- so we only get frame-late input when mouse is outside the area

--]]

local Plugin = script:FindFirstAncestorWhichIsA("Plugin")
local Fusion = require(Plugin:FindFirstChild("Fusion", true))

local getState = require(script.Parent.getState)
local unwrap = require(script.Parent.unwrap)
local types = require(script.Parent.types)

local New = Fusion.New
local Value = Fusion.Value
local Computed = Fusion.Computed
local Children = Fusion.Children
local OnEvent = Fusion.OnEvent
local Hydrate = Fusion.Hydrate
local Observer = Fusion.Observer
local Cleanup = Fusion.Cleanup

type vector2Value = types.Value<Vector2>
type vector2Input = (Vector2 | vector2Value)?

type DragInputProperites = {
	Instance: GuiObject,
	Enabled: (boolean | types.StateObject<boolean>)?,
	OnChange: ((newValue: Vector2) -> Vector2?)?,
	Value: vector2Input,
	Min: vector2Input,
	Max: vector2Input,
	Step: vector2Input,
}

return function(props: DragInputProperites): (vector2Value, types.Computed<Vector2>, types.Value<boolean>)
	local isEnabled = getState(props.Enabled, true)
	local isDragging = Value(false)

	local connectionProvider = props.Instance
	local globalConnection = nil

	local currentValue = getState(props.Value, Vector2.new(0, 0), "Value")
	local maxValue = getState(props.Max, Vector2.new(1, 1))
	local minValue = getState(props.Min, Vector2.new(0, 0))

	local range = Computed(function()
		return unwrap(maxValue) - unwrap(minValue)
	end)

	local currentAlpha = Computed(function()
		return (unwrap(currentValue) - unwrap(minValue)) / unwrap(range)
	end)

	local function processInput(position)
		local connectionProvider = unwrap(connectionProvider, false)
		if connectionProvider then
			local offset = position - connectionProvider.AbsolutePosition
			local alpha = offset / connectionProvider.AbsoluteSize

			local range = unwrap(range, false)
			local step = unwrap(props.Step or Vector2.new(-1, -1), false)

			local value = range * alpha
			value = Vector2.new(
				if step.X>0 then math.round(value.X / step.X) * step.X else value.X,
				if step.Y>0 then math.round(value.Y / step.Y) * step.Y else value.Y
			)

			value = Vector2.new(
				math.clamp(value.X, 0, range.X) + unwrap(minValue, false).X,
				math.clamp(value.Y, 0, range.Y) + unwrap(minValue, false).Y
			)

			if value ~= unwrap(currentValue) then
				if props.OnChange then
					local newValue = props.OnChange(value)
					currentValue:set(if newValue~=nil then newValue else value)
				else
					currentValue:set(value)
				end
			end
		end
	end

	local function onDragStart(inputObject)
		local connectionProvider = unwrap(connectionProvider)
		local currentlyDragging = unwrap(isDragging)
		if not unwrap(isEnabled) or currentlyDragging or inputObject.UserInputType ~= Enum.UserInputType.MouseButton1 or connectionProvider==nil or globalConnection then
			return
		end

		isDragging:set(true)
		processInput(Vector2.new(inputObject.Position.X, inputObject.Position.Y))

		local widget = connectionProvider:FindFirstAncestorWhichIsA("DockWidgetPluginGui")
		if widget ~= nil then
			globalConnection = game:GetService("RunService").Heartbeat:Connect(function()
				processInput(widget:GetRelativeMousePosition())
			end)
		else
			globalConnection = game:GetService("UserInputService").InputChanged:Connect(function(newInput)
				if newInput.UserInputType == Enum.UserInputType.MouseMovement then
					processInput(Vector2.new(newInput.Position.x, newInput.Position.y))
				end
			end)
		end
	end

	local function cleanupGlobalConnection()
		if globalConnection then
			globalConnection:Disconnect()
			globalConnection = nil
		end
	end

	local tasks = {}
	local function cleanupTasks()
		for _,cleanupTask in pairs(tasks) do
			cleanupTask()
		end
	end

	table.insert(tasks, Observer(props.Instance):onChange(function()
		local connectionProvider = unwrap(connectionProvider, false)
		if connectionProvider == nil then
			cleanupTasks()
		else
			table.insert(tasks, cleanupGlobalConnection)

			Hydrate(connectionProvider)({
				[Cleanup] = cleanupTasks,
				[OnEvent "InputBegan"] = onDragStart,
				[OnEvent "InputEnded"] = function(inputObject)
					if inputObject.UserInputType == Enum.UserInputType.MouseButton1 then
						isDragging:set(false)
						cleanupGlobalConnection()
					end
				end,
				[OnEvent "InputChanged"] = function(inputObject)
					if unwrap(isDragging) then
						processInput(Vector2.new(inputObject.Position.X, inputObject.Position.Y))
					end
				end,
			})
		end
	end))

	return currentValue, currentAlpha, isDragging
end
