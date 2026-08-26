--!strict

local State = require(script.Parent.Parent.Parent.state)
local Fusion = require(script.Parent.Parent.Parent.Packages.Fusion)

local New = Fusion.New
local Value = Fusion.Value
local Computed = Fusion.Computed
local Observer = Fusion.Observer
local Children = Fusion.Children
local OnEvent = Fusion.OnEvent
local Cleanup = Fusion.Cleanup
local Ref = Fusion.Ref

local StudioComponents = script.Parent.Parent.Parent.Components.StudioComponents
local themeProvider = require(StudioComponents.Util.themeProvider)
local Checkbox = require(StudioComponents.Checkbox)
local LimitedTextInput = require(StudioComponents.LimitedTextInput)
local Slider = require(StudioComponents.Slider)
local SharedComponents = require(script.Parent.Parent.SharedComponents)
local getMotionState = require(StudioComponents.Util.getMotionState)
local HttpService = game:GetService("HttpService")

local AnimationModifierStack = {}

local MODIFIER_DEFINITIONS = {
	{ kind = "time_scale", name = "Animation Resizer", category = "Animation" },
	{ kind = "speed", name = "Speed", category = "Animation" },
	{ kind = "mirror", name = "Mirror Animation", category = "Animation" },
	{ kind = "resample", name = "Resample FPS", category = "Animation" },
	{ kind = "simplifier", name = "Keyframe Simplifier", category = "Optimization" },
}

local MODIFIER_NAMES: { [string]: string } = {}
local MODIFIER_CATEGORIES: { [string]: string } = {}
local MODIFIER_HINTS = {
	time_scale = "Resizes the animation by a given factor. Useful for scaling animations up or down.",
	speed = "Changes animation timing without changing its poses.",
	mirror = "Swaps left and right bones while reflecting their local transforms.",
	resample = "Samples poses at a fixed rate and holds them between samples.",
	simplifier = "Thins keyframes to reduce animation size for lower in-game bandwidth.",
}

local modifierCardBackground = Computed(function()
	local mainBackground = themeProvider:GetColor(Enum.StudioStyleGuideColor.MainBackground):get()
	local contrastTarget = if themeProvider.IsDark:get() then Color3.new(0, 0, 0) else Color3.new(1, 1, 1)
	return mainBackground:Lerp(contrastTarget, 0.18)
end)

local function getAnimSizeString(animData: any?): string
	if not animData then
		return "N/A"
	end

	local success, encoded = pcall(function()
		return HttpService:JSONEncode(animData)
	end)
	if not success or not encoded then
		return "N/A"
	end

	local bytes = #encoded
	-- ExportAnimation sends this exact, uncompressed JSON string as its HTTP body.
	return string.format("%.2f KB (%d B)", bytes / 1024, bytes)
end

local function getSimplifierKeepRatio(strength: number): number
	local t = math.clamp(strength / 100, 0, 1)
	return math.clamp(1 - 0.75 * (t ^ 0.9), 0.25, 1)
end
for _, definition in ipairs(MODIFIER_DEFINITIONS) do
	MODIFIER_NAMES[definition.kind] = definition.name
	MODIFIER_CATEGORIES[definition.kind] = definition.category
end

local function cloneStack(): { any }
	local copy = {}
	for index, modifier in ipairs(State.animationModifierStack:get()) do
		copy[index] = table.clone(modifier)
	end
	return copy
end

local function publishStack(stack: { any })
	State.animationModifierStack:set(stack)

	local hasTimeScale = false
	local hasSpeed = false
	local hasMirror = false
	local hasResample = false
	local hasSimplifier = false
	for _, modifier in ipairs(stack) do
		if modifier.kind == "time_scale" then
			hasTimeScale = true
			State.scaleFactor:set(if modifier.enabled then modifier.factor or 1 else 1)
		elseif modifier.kind == "speed" then
			hasSpeed = true
			State.speedEnabled:set(modifier.enabled == true)
			State.speedMultiplier:set(modifier.speed or 1)
		elseif modifier.kind == "mirror" then
			hasMirror = true
			State.mirrorAnimationEnabled:set(modifier.enabled == true)
		elseif modifier.kind == "resample" then
			hasResample = true
			State.resampleEnabled:set(modifier.enabled == true)
			State.resampleFps:set(modifier.fps or 24)
		elseif modifier.kind == "simplifier" then
			hasSimplifier = true
			State.simplifierEnabled:set(modifier.enabled == true)
			State.simplifierStrength:set(modifier.strength or 15)
		end
	end

	if not hasTimeScale then
		State.scaleFactor:set(1)
	end
	if not hasSpeed then
		State.speedEnabled:set(false)
		State.speedMultiplier:set(1)
	end
	if not hasMirror then
		State.mirrorAnimationEnabled:set(false)
	end
	if not hasResample then
		State.resampleEnabled:set(false)
	end
	if not hasSimplifier then
		State.simplifierEnabled:set(false)
	end
end

local function updateModifier(id: string, changes: any)
	local stack = cloneStack()
	for _, modifier in ipairs(stack) do
		if modifier.id == id then
			for key, value in pairs(changes) do
				modifier[key] = value
			end
			break
		end
	end
	publishStack(stack)
end

local function removeModifier(index: number)
	local stack = cloneStack()
	table.remove(stack, index)
	publishStack(stack)
end

local function headerButton(
	text: any,
	width: number,
	layoutOrder: number,
	activated: () -> ()
): TextButton
	local hovered = Value(false)
	return New("TextButton")({
		Text = text,
		LayoutOrder = layoutOrder,
		Size = UDim2.fromOffset(width, 28),
		BackgroundColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.HeaderSection),
		BackgroundTransparency = Computed(function()
			return if hovered:get() then 0.45 else 1
		end),
		BorderSizePixel = 0,
		AutoButtonColor = false,
		Font = themeProvider:GetFont("Bold"),
		TextSize = 14,
		TextColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.BrightText),
		[OnEvent("MouseEnter")] = function()
			hovered:set(true)
		end,
		[OnEvent("MouseLeave")] = function()
			hovered:set(false)
		end,
		[OnEvent("Activated")] = activated,
	})
end

local function collapseButton(expanded: boolean, activated: () -> ()): TextButton
	local hovered = Value(false)
	return New("TextButton")({
		Text = "",
		Size = UDim2.fromOffset(20, 28),
		LayoutOrder = 1,
		BackgroundTransparency = Computed(function()
			return if hovered:get() then 0.45 else 1
		end),
		BackgroundColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.HeaderSection),
		BorderSizePixel = 0,
		AutoButtonColor = false,
		[OnEvent("MouseEnter")] = function()
			hovered:set(true)
		end,
		[OnEvent("MouseLeave")] = function()
			hovered:set(false)
		end,
		[OnEvent("Activated")] = activated,
		[Children] = New("ImageLabel")({
			AnchorPoint = Vector2.new(0.5, 0.5),
			Position = UDim2.fromScale(0.5, 0.5),
			Size = UDim2.fromOffset(9, 9),
			Image = "rbxassetid://5607705156",
			ImageRectSize = Vector2.new(10, 10),
			ImageRectOffset = if expanded then Vector2.new(10, 0) else Vector2.new(0, 0),
			ImageColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.BrightText),
			BackgroundTransparency = 1,
		}),
	})
end

local function numericScrubInput(props: {
	Value: any,
	Enabled: boolean,
	Increment: number,
	OnChange: (number) -> (),
	LayoutOrder: number,
	Size: UDim2,
}): TextBox
	local value = props.Value
	local text = Value(string.format("%.2f", value:get(false)))
	local textBoxRef = Value()
	local isEditing = Value(false)
	local isTextEditable = Value(false)
	local isHovering = Value(false)
	local isMouseDown = false
	local isDragging = false
	local dragStartX = 0
	local dragStartValue = 0
	local dragConnection: RBXScriptConnection? = nil
	local endConnection: RBXScriptConnection? = nil
	local userInputService = game:GetService("UserInputService")
	local runService = game:GetService("RunService")

	local function getIncrement(): number
		-- Each modifier defines its own base increment; shift remains five times
		-- coarser for quick larger changes.
		if userInputService:IsKeyDown(Enum.KeyCode.LeftShift) or userInputService:IsKeyDown(Enum.KeyCode.RightShift) then
			return props.Increment * 5
		end
		return props.Increment
	end

	local function disconnectDrag()
		if dragConnection then
			dragConnection:Disconnect()
			dragConnection = nil
		end
		if endConnection then
			endConnection:Disconnect()
			endConnection = nil
		end
	end

	local function setValue(nextValue: number, increment: number?)
		if increment then
			nextValue = math.round(nextValue / increment) * increment
		end
		if nextValue <= 0 then
			return
		end
		text:set(string.format("%.2f", nextValue))
		props.OnChange(nextValue)
	end

	local function finishDrag()
		if not isMouseDown then
			return
		end

		local didDrag = isDragging
		isMouseDown = false
		isDragging = false
		disconnectDrag()
		if didDrag then
			isEditing:set(false)
			isTextEditable:set(false)
			return
		end

		local textBox = textBoxRef:get()
		if textBox then
			isTextEditable:set(true)
			textBox:CaptureFocus()
		end
	end

	local cleanupValueObserver = Observer(value):onChange(function()
		if not isEditing:get() then
			text:set(string.format("%.2f", value:get(false)))
		end
	end)

	local function arrowButton(textValue: string, position: UDim2, direction: number): TextButton
		return New("TextButton")({
			AnchorPoint = Vector2.new(if direction < 0 then 0 else 1, 0.5),
			Position = position,
			Size = UDim2.fromOffset(18, 20),
			ZIndex = 2,
			Visible = Computed(function()
				return props.Enabled and isHovering:get()
			end),
			BackgroundTransparency = 1,
			BorderSizePixel = 0,
			AutoButtonColor = false,
			Font = themeProvider:GetFont("Bold"),
			Text = textValue,
			TextSize = 12,
			TextColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.MainText),
			[OnEvent("Activated")] = function()
				local increment = getIncrement()
				setValue(math.max(0.01, value:get(false) + direction * increment), increment)
			end,
		})
	end

	return New("TextBox")({
		LayoutOrder = props.LayoutOrder,
		Size = props.Size,
		BackgroundColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.InputFieldBackground),
		BorderSizePixel = 0,
		ClearTextOnFocus = false,
		TextEditable = isTextEditable,
		Font = themeProvider:GetFont("Default"),
		Text = text,
		TextSize = 12,
		TextXAlignment = Enum.TextXAlignment.Center,
		TextColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.MainText),
		TextTransparency = if props.Enabled then 0 else 0.5,
		[Ref] = textBoxRef,
		[OnEvent("Focused")] = function()
			isEditing:set(true)
		end,
		[OnEvent("MouseEnter")] = function()
			isHovering:set(true)
		end,
		[OnEvent("MouseLeave")] = function()
			isHovering:set(false)
		end,
		[OnEvent("FocusLost")] = function()
			if not isMouseDown and not isDragging then
				local textBox = textBoxRef:get()
				local nextValue = textBox and tonumber(textBox.Text) or nil
				if nextValue then
					setValue(nextValue)
				else
					text:set(string.format("%.2f", value:get(false)))
				end
			end
			isEditing:set(false)
			isTextEditable:set(false)
		end,
		[OnEvent("InputEnded")] = function(input)
			if input.UserInputType == Enum.UserInputType.MouseButton1 then
				finishDrag()
			end
		end,
		[OnEvent("InputBegan")] = function(input)
			if not props.Enabled then
				return
			end
			if input.UserInputType == Enum.UserInputType.Keyboard and isEditing:get() then
				local increment = getIncrement()
				if input.KeyCode == Enum.KeyCode.Up then
					setValue(value:get(false) + increment, increment)
				elseif input.KeyCode == Enum.KeyCode.Down then
					setValue(math.max(0.01, value:get(false) - increment), increment)
				end
				return
			end
			if input.UserInputType ~= Enum.UserInputType.MouseButton1 then
				return
			end

			disconnectDrag()
			-- A press only starts a gesture. The first real mouse delta decides
			-- whether it becomes a drag; otherwise releasing enters text editing.
			isMouseDown = true
			isDragging = false
			isEditing:set(false)
			isTextEditable:set(false)
			local textBox = textBoxRef:get()
			if textBox then
				textBox:ReleaseFocus()
			end

			local widget = textBox and textBox:FindFirstAncestorWhichIsA("DockWidgetPluginGui")
			local function pointerX(inputObject: InputObject?): number
				if widget then
					return widget:GetRelativeMousePosition().X
				end
				return inputObject and inputObject.Position.X or 0
			end

			dragStartX = pointerX(input)
			dragStartValue = value:get(false)
			local function updateDrag(currentX: number)
				if not isMouseDown then
					return
				end
				local deltaX = currentX - dragStartX
				if deltaX == 0 then
					return
				end
				if not isDragging then
					isDragging = true
					local textBox = textBoxRef:get()
					if textBox then
						textBox:ReleaseFocus()
					end
				end
				local increment = getIncrement()
				-- Four pixels per increment keeps drags precise while allowing each
				-- modifier to control its own scale through Increment.
				setValue(math.max(0.01, dragStartValue + deltaX * increment / 4), increment)
			end

			if widget then
				dragConnection = runService.Heartbeat:Connect(function()
					updateDrag(pointerX(nil))
				end)
			else
				dragConnection = userInputService.InputChanged:Connect(function(changedInput)
					if changedInput.UserInputType == Enum.UserInputType.MouseMovement then
						updateDrag(pointerX(changedInput))
					end
				end)
			end
			endConnection = game:GetService("UserInputService").InputEnded:Connect(function(endedInput)
				if endedInput.UserInputType == Enum.UserInputType.MouseButton1 then
					finishDrag()
				end
			end)
		end,
		[Cleanup] = function()
			disconnectDrag()
			cleanupValueObserver()
		end,
		[Children] = {
			New("UICorner")({ CornerRadius = UDim.new(0, 3) }),
			arrowButton("<", UDim2.new(0, 2, 0.5, 0), -1),
			arrowButton(">", UDim2.new(1, -2, 0.5, 0), 1),
		},
	})
end

local function addMenuButton(text: string, layoutOrder: number, activated: () -> ()): TextButton
	local hovered = Value(false)
	return New("TextButton")({
		Text = text,
		LayoutOrder = layoutOrder,
		Size = UDim2.new(1, 0, 0, 26),
		BackgroundColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.Button),
		BackgroundTransparency = Computed(function()
			return if hovered:get() then 0.2 else 0.55
		end),
		BorderSizePixel = 0,
		AutoButtonColor = false,
		Font = themeProvider:GetFont("Default"),
		TextSize = 13,
		TextXAlignment = Enum.TextXAlignment.Left,
		TextColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.ButtonText),
		[OnEvent("MouseEnter")] = function()
			hovered:set(true)
		end,
		[OnEvent("MouseLeave")] = function()
			hovered:set(false)
		end,
		[OnEvent("Activated")] = activated,
		[Children] = {
			New("UIPadding")({ PaddingLeft = UDim.new(0, 8) }),
			New("UICorner")({ CornerRadius = UDim.new(0, 3) }),
		},
	})
end

function AnimationModifierStack.create(services: any): Frame
	local addMenuOpen = Value(false)
	local searchText = Value("")
	local activeHint = Value("")
	local debounceThread: thread? = nil

	local function refreshAnimation()
		if debounceThread then
			task.cancel(debounceThread)
		end
		debounceThread = task.delay(0.12, function()
			debounceThread = nil
			if services and services.animationManager then
				services.animationManager:resimplifyAndPlay()
			end
		end)
	end

	local simplifierPercentageText = Computed(function()
		return string.format("%d%%", State.simplifierStrength:get())
	end)
	local resizerInfoText = Computed(function()
		return string.format(
			"Scale Factor: %.2f | Model Scale: %.2f",
			State.scaleFactor:get(),
			State.rigScale:get()
		)
	end)
	local simplifierInfoText = Computed(function()
		local strength = State.simplifierStrength:get()
		local rawData = State.lastRawAnimData:get()
		local simplifiedData = State.currentAnimationData:get()
		local sizeText = "Payload: " .. getAnimSizeString(rawData)
		if simplifiedData and simplifiedData ~= rawData then
			sizeText ..= " -> " .. getAnimSizeString(simplifiedData)
		end
		return string.format(
			"Strength: %d%% | Keeps ~%.0f%% | %s",
			strength,
			getSimplifierKeepRatio(strength) * 100,
			sizeText
		)
	end)

	local function availableModifierKinds(query: string?): { string }
		local normalizedQuery = string.lower(query or "")
		local present: { [string]: boolean } = {}
		for _, modifier in ipairs(State.animationModifierStack:get()) do
			present[modifier.kind] = true
		end

		local result = {}
		for _, definition in ipairs(MODIFIER_DEFINITIONS) do
			if
				not present[definition.kind]
				and (normalizedQuery == "" or string.find(string.lower(definition.name), normalizedQuery, 1, true))
			then
				table.insert(result, definition.kind)
			end
		end
		return result
	end

	local function addModifier(kind: string)
		for _, modifier in ipairs(State.animationModifierStack:get()) do
			if modifier.kind == kind then
				return
			end
		end

		local stack = cloneStack()
		table.insert(stack, {
			id = kind,
			kind = kind,
			enabled = true,
			expanded = true,
			factor = if kind == "time_scale" then 1 else nil,
			speed = if kind == "speed" then 1 else nil,
			mirror = if kind == "mirror" then true else nil,
			fps = if kind == "resample" then 24 else nil,
			strength = if kind == "simplifier" then 15 else nil,
		})
		publishStack(stack)
		State.animationDirty:set(true)
		addMenuOpen:set(false)
		searchText:set("")
		refreshAnimation()
	end

	local function createModifierCard(modifier: any, index: number): Frame
		local hasSettings = modifier.kind ~= "mirror"
		local expanded = hasSettings and modifier.expanded ~= false
		local enabled = modifier.enabled == true
		local isHovering = Value(false)
		local cardHeight = getMotionState(Computed(function()
			return UDim2.new(1, 0, 0, if expanded then 88 else 30)
		end), "Spring", 35)
		local settingsChildren = {}
		if modifier.kind == "time_scale" then
			table.insert(
				settingsChildren,
				New("TextLabel")({
					Text = "Factor",
					LayoutOrder = 1,
					Size = UDim2.new(0.4, 0, 0, 24),
					BackgroundTransparency = 1,
					Font = themeProvider:GetFont("Default"),
					TextSize = 12,
					TextXAlignment = Enum.TextXAlignment.Left,
					TextTransparency = if enabled then 0 else 0.5,
					TextColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.MainText),
				})
			)
			table.insert(
				settingsChildren,
				numericScrubInput({
					LayoutOrder = 2,
					Size = UDim2.new(0.6, -4, 0, 24),
					Value = State.scaleFactor,
					Enabled = enabled,
					Increment = 0.01,
					OnChange = function(value)
						modifier.factor = value
						State.scaleFactor:set(value)
						State.animationDirty:set(true)
						if services and services.playbackService then
							services.playbackService:playAllRigs()
						end
					end,
				})
			)
		elseif modifier.kind == "speed" then
			table.insert(
				settingsChildren,
				New("TextLabel")({
					Text = "Multiplier",
					LayoutOrder = 1,
					Size = UDim2.new(0.4, 0, 0, 24),
					BackgroundTransparency = 1,
					Font = themeProvider:GetFont("Default"),
					TextSize = 12,
					TextXAlignment = Enum.TextXAlignment.Left,
					TextTransparency = if enabled then 0 else 0.5,
					TextColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.MainText),
				})
			)
			table.insert(
				settingsChildren,
				numericScrubInput({
					LayoutOrder = 2,
					Size = UDim2.new(0.6, -4, 0, 24),
					Value = State.speedMultiplier,
					Enabled = enabled,
					Increment = 0.05,
					OnChange = function(value)
						local speed = math.clamp(value, 0.05, 10)
						modifier.speed = speed
						State.speedMultiplier:set(speed)
						State.animationDirty:set(true)
						refreshAnimation()
					end,
				})
			)
		elseif modifier.kind == "resample" then
			table.insert(
				settingsChildren,
				New("TextLabel")({
					Text = "FPS",
					LayoutOrder = 1,
					Size = UDim2.new(0.4, 0, 0, 24),
					BackgroundTransparency = 1,
					Font = themeProvider:GetFont("Default"),
					TextSize = 12,
					TextXAlignment = Enum.TextXAlignment.Left,
					TextTransparency = if enabled then 0 else 0.5,
					TextColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.MainText),
				})
			)
			table.insert(
				settingsChildren,
				numericScrubInput({
					LayoutOrder = 2,
					Size = UDim2.new(0.6, -4, 0, 24),
					Value = State.resampleFps,
					Enabled = enabled,
					Increment = 1,
					OnChange = function(value)
						local fps = math.clamp(math.round(value), 1, 240)
						modifier.fps = fps
						State.resampleFps:set(fps)
						State.animationDirty:set(true)
						refreshAnimation()
					end,
				})
			)
		elseif modifier.kind == "simplifier" then
			table.insert(
				settingsChildren,
				New("TextLabel")({
					Text = "Strength",
					LayoutOrder = 1,
					Size = UDim2.fromOffset(54, 20),
					BackgroundTransparency = 1,
					Font = themeProvider:GetFont("Default"),
					TextSize = 12,
					TextXAlignment = Enum.TextXAlignment.Left,
					TextTransparency = if enabled then 0 else 0.5,
					TextColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.MainText),
				})
			)
			table.insert(
				settingsChildren,
				Slider({
					LayoutOrder = 2,
					Size = UDim2.new(1, -106, 0, 20),
					Min = 0,
					Max = 100,
					Step = 1,
					Value = State.simplifierStrength,
					Enabled = enabled,
					OnChange = function(value)
						modifier.strength = value
						State.simplifierStrength:set(value)
						State.animationDirty:set(true)
						refreshAnimation()
					end,
				} :: any)
			)
			table.insert(
				settingsChildren,
				New("TextLabel")({
					Text = simplifierPercentageText,
					LayoutOrder = 3,
					Size = UDim2.fromOffset(44, 20),
					BackgroundColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.InputFieldBackground),
					BorderSizePixel = 0,
					Font = themeProvider:GetFont("Default"),
					TextSize = 12,
					TextXAlignment = Enum.TextXAlignment.Center,
					TextTransparency = if enabled then 0 else 0.5,
					TextColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.MainText),
					[Children] = New("UICorner")({ CornerRadius = UDim.new(0, 3) }),
				})
			)
		end

		local function changed()
			State.animationDirty:set(true)
			refreshAnimation()
		end

		local infoText = if modifier.kind == "time_scale"
			then resizerInfoText
			elseif modifier.kind == "speed" then string.format("Playback: %.2fx", State.speedMultiplier:get())
			elseif modifier.kind == "mirror" then "Mirrors across the rig's left/right axis"
			elseif modifier.kind == "resample" then string.format("Holds poses at %d FPS", State.resampleFps:get())
			else simplifierInfoText

		return New("Frame")({
			Name = modifier.id,
			LayoutOrder = index,
			Size = cardHeight,
			ZIndex = 1,
			BackgroundColor3 = modifierCardBackground,
			BorderSizePixel = 0,
			ClipsDescendants = false,
			[Children] = {
				New("UICorner")({ CornerRadius = UDim.new(0, 4) }),
				New("UIStroke")({
					Color = themeProvider:GetColor(Enum.StudioStyleGuideColor.Border),
					Transparency = 0.25,
					Thickness = 1,
				}),
				New("Frame")({
					Size = UDim2.new(1, 0, 0, 30),
					BackgroundColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.HeaderSection),
					BackgroundTransparency = getMotionState(Computed(function()
						return if isHovering:get() then 0.12 else 0
					end), "Spring", 40),
					BorderSizePixel = 0,
					[OnEvent("MouseEnter")] = function()
						isHovering:set(true)
					end,
					[OnEvent("MouseLeave")] = function()
						isHovering:set(false)
					end,
					[Children] = {
						New("UICorner")({ CornerRadius = UDim.new(0, 4) }),
						New("UIStroke")({
							Color = themeProvider:GetColor(Enum.StudioStyleGuideColor.Border),
							Transparency = 0.35,
						}),
						New("UIListLayout")({
							FillDirection = Enum.FillDirection.Horizontal,
							VerticalAlignment = Enum.VerticalAlignment.Center,
							SortOrder = Enum.SortOrder.LayoutOrder,
						}),
						if hasSettings
							then collapseButton(expanded, function()
								updateModifier(modifier.id, { expanded = not expanded })
							end)
							else nil,
						New("TextButton")({
							Text = MODIFIER_NAMES[modifier.kind] or modifier.kind,
							LayoutOrder = 2,
							Size = UDim2.new(1, if hasSettings then -68 else -48, 0, 22),
							BackgroundTransparency = 1,
							BorderSizePixel = 0,
							AutoButtonColor = false,
							Font = themeProvider:GetFont("Bold"),
							TextSize = 12,
							TextXAlignment = Enum.TextXAlignment.Left,
							TextTransparency = if enabled then 0 else 0.45,
							TextColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.BrightText),
							[OnEvent("Activated")] = if hasSettings
								then function()
									updateModifier(modifier.id, { expanded = not expanded })
								end
								else function() end,
							[OnEvent("MouseEnter")] = function()
								activeHint:set(MODIFIER_HINTS[modifier.kind] or "")
							end,
							[OnEvent("MouseLeave")] = function()
								activeHint:set("")
							end,
							[Children] = {
								New("UIPadding")({ PaddingLeft = UDim.new(0, 2) }),
							},
						}),
						New("Frame")({
							LayoutOrder = 3,
							Size = UDim2.fromOffset(26, 28),
							BackgroundTransparency = 1,
							[Children] = Checkbox({
								Position = UDim2.fromOffset(6, 7),
								Size = UDim2.fromOffset(15, 15),
								Value = enabled,
								OnChange = function(value: boolean)
									updateModifier(modifier.id, { enabled = value })
									changed()
								end,
							} :: any),
						}),
						headerButton("x", 22, 4, function()
							removeModifier(index)
							changed()
						end),
					},
				}),
				New("Frame")({
					Position = UDim2.fromOffset(8, 35),
					Size = UDim2.new(1, -16, 0, 24),
					Visible = expanded,
					BackgroundTransparency = 1,
					[Children] = {
						New("UIListLayout")({
							FillDirection = Enum.FillDirection.Horizontal,
							VerticalAlignment = Enum.VerticalAlignment.Center,
							SortOrder = Enum.SortOrder.LayoutOrder,
							Padding = UDim.new(0, 4),
						}),
						settingsChildren,
					},
				}),
				New("TextLabel")({
					Position = UDim2.fromOffset(8, 61),
					Size = UDim2.new(1, -16, 0, 19),
					Visible = expanded,
					BackgroundTransparency = 1,
					Font = themeProvider:GetFont("Default"),
					Text = infoText,
					TextSize = 11,
					TextTruncate = Enum.TextTruncate.AtEnd,
					TextTransparency = if enabled then 0.2 else 0.55,
					TextXAlignment = Enum.TextXAlignment.Left,
					TextColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.DimmedText),
				}),
			},
		})
	end

	local cards = Computed(function()
		local stack = State.animationModifierStack:get()
		local result = {}
		for index, modifier in ipairs(stack) do
			table.insert(result, createModifierCard(modifier, index))
		end
		return result
	end)

	local menuItems = Computed(function()
		local result = {}
		local layoutOrder = 1
		local lastCategory: string? = nil
		for _, kind in ipairs(availableModifierKinds(searchText:get())) do
			local category = MODIFIER_CATEGORIES[kind]
			if category ~= lastCategory then
				table.insert(
					result,
					New("TextLabel")({
						Text = category,
						LayoutOrder = layoutOrder,
						Size = UDim2.new(1, 0, 0, 18),
						BackgroundTransparency = 1,
						Font = themeProvider:GetFont("SemiBold"),
						TextSize = 11,
						TextXAlignment = Enum.TextXAlignment.Left,
						TextColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.DimmedText),
					})
				)
				layoutOrder += 1
				lastCategory = category
			end
			table.insert(
				result,
				addMenuButton(MODIFIER_NAMES[kind], layoutOrder, function()
					addModifier(kind)
				end)
			)
			layoutOrder += 1
		end
		if #result == 0 then
			table.insert(
				result,
				New("TextLabel")({
					Text = "No matching modifiers",
					Size = UDim2.new(1, 0, 0, 26),
					BackgroundTransparency = 1,
					Font = themeProvider:GetFont("Default"),
					TextSize = 12,
					TextColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.DimmedText),
				})
			)
		end
		return result
	end)

	local addButtonHovered = Value(false)
	local hasAvailableModifiers = Computed(function()
		State.animationModifierStack:get()
		return #availableModifierKinds() > 0
	end)

	return New("Frame")({
		Name = "AnimationModifierStack",
		Size = UDim2.new(1, 0, 0, 0),
		AutomaticSize = Enum.AutomaticSize.Y,
		BackgroundTransparency = 1,
		[Children] = {
			New("UIListLayout")({
				SortOrder = Enum.SortOrder.LayoutOrder,
				Padding = UDim.new(0, 5),
			}),
			New("Frame")({
				LayoutOrder = 3,
				Size = UDim2.new(1, 0, 0, 0),
				AutomaticSize = Enum.AutomaticSize.Y,
				BackgroundTransparency = 1,
				[Children] = {
					New("UIListLayout")({ SortOrder = Enum.SortOrder.LayoutOrder, Padding = UDim.new(0, 4) }),
					cards,
				},
			}),
			New("TextButton")({
				Text = "+  Add Modifier",
				LayoutOrder = 1,
				Size = UDim2.new(1, 0, 0, 27),
				BackgroundColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.Button),
				BackgroundTransparency = Computed(function()
					if not hasAvailableModifiers:get() then
						return 0.75
					end
					return if addButtonHovered:get() then 0.25 else 0.5
				end),
				BorderSizePixel = 0,
				Active = hasAvailableModifiers,
				AutoButtonColor = false,
				Font = themeProvider:GetFont("SemiBold"),
				TextSize = 12,
				TextTransparency = Computed(function()
					return if hasAvailableModifiers:get() then 0 else 0.5
				end),
				TextColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.ButtonText),
				[OnEvent("MouseEnter")] = function()
					addButtonHovered:set(true)
				end,
				[OnEvent("MouseLeave")] = function()
					addButtonHovered:set(false)
				end,
				[OnEvent("Activated")] = function()
					if hasAvailableModifiers:get() then
						addMenuOpen:set(not addMenuOpen:get())
					end
				end,
				[Children] = New("UICorner")({ CornerRadius = UDim.new(0, 4) }),
			}),
			New("Frame")({
				LayoutOrder = 2,
				Size = Computed(function()
					if not addMenuOpen:get() then
						return UDim2.new(1, 0, 0, 0)
					end
					local kinds = availableModifierKinds(searchText:get())
					if #kinds == 0 then
						return UDim2.new(1, 0, 0, 68)
					end
					local categories: { [string]: boolean } = {}
					for _, kind in ipairs(kinds) do
						categories[MODIFIER_CATEGORIES[kind]] = true
					end
					local categoryCount = 0
					for _ in pairs(categories) do
						categoryCount += 1
					end
					return UDim2.new(1, 0, 0, 39 + #kinds * 29 + categoryCount * 21)
				end),
				Visible = addMenuOpen,
				ClipsDescendants = true,
				BackgroundColor3 = themeProvider:GetColor(Enum.StudioStyleGuideColor.MainBackground),
				BorderSizePixel = 0,
				[Children] = {
					New("UICorner")({ CornerRadius = UDim.new(0, 4) }),
					New("UIStroke")({
						Color = themeProvider:GetColor(Enum.StudioStyleGuideColor.Border),
						Transparency = 0.25,
					}),
					New("UIPadding")({
						PaddingTop = UDim.new(0, 6),
						PaddingRight = UDim.new(0, 6),
						PaddingBottom = UDim.new(0, 6),
						PaddingLeft = UDim.new(0, 6),
					}),
					New("UIListLayout")({
						SortOrder = Enum.SortOrder.LayoutOrder,
						Padding = UDim.new(0, 3),
					}),
					LimitedTextInput({
						PlaceholderText = "Search modifiers",
						Text = searchText,
						LayoutOrder = 1,
						Size = UDim2.new(1, 0, 0, 27),
						GraphemeLimit = 32,
						OnChange = function(text)
							searchText:set(text)
						end,
					} :: any),
					New("Frame")({
						LayoutOrder = 2,
						Size = UDim2.new(1, 0, 0, 0),
						AutomaticSize = Enum.AutomaticSize.Y,
						BackgroundTransparency = 1,
						[Children] = {
							New("UIListLayout")({
								SortOrder = Enum.SortOrder.LayoutOrder,
								Padding = UDim.new(0, 3),
							}),
							menuItems,
						},
					}),
				},
			}),
			SharedComponents.AnimatedHintLabel({
				Text = activeHint,
				LayoutOrder = 4,
				Size = UDim2.new(1, 0, 0, 0),
				TextWrapped = true,
				ClipsDescendants = true,
				Visible = true,
				TextTransparency = 0,
			}),
		},
	})
end

return AnimationModifierStack
