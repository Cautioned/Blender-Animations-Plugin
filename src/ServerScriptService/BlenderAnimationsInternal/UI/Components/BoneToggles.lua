--!native
--!strict
--!optimize 2

local State = require(script.Parent.Parent.Parent.state)
local Fusion = require(script.Parent.Parent.Parent.Packages.Fusion)
local UserInputService = game:GetService("UserInputService")
local StudioService = game:GetService("StudioService")

local New = Fusion.New
local Children = Fusion.Children
local Computed = Fusion.Computed
local Value = Fusion.Value
local OnEvent = Fusion.OnEvent

local StudioComponents = script.Parent.Parent.Parent.Components:FindFirstChild("StudioComponents")
local Checkbox = require(StudioComponents.Checkbox)
local VerticalCollapsibleSection = require(StudioComponents.VerticalCollapsibleSection)

local StudioComponentsUtil = StudioComponents:FindFirstChild("Util")
local themeProvider = require(StudioComponentsUtil.themeProvider)
local constants = require(StudioComponentsUtil.constants)

local BoneToggles = {}

local function copyBoneWeights(list: { any })
	local out = table.create(#list)
	for i, entry in ipairs(list) do
		out[i] = table.clone(entry)
	end
	return out
end

local boneWeightsUpdateQueued = false
local function scheduleBoneWeightsUpdate(boneWeights: { any })
	if boneWeightsUpdateQueued then
		return
	end
	boneWeightsUpdateQueued = true
	task.defer(function()
		boneWeightsUpdateQueued = false
		State.boneWeights:set(copyBoneWeights(boneWeights))
	end)
end

local function getBoneIconData(boneName: string)
	local className = "Bone"
	if State.activeRig and State.activeRig.bones then
		local rigBone = State.activeRig.bones[boneName]
		if rigBone and rigBone.joint then
			className = "Motor6D"
		end
	end

	local ok, iconData = pcall(function()
		return StudioService:GetClassIcon(className)
	end)

	if ok then
		return iconData
	end

	return nil
end

local function syncRigBoneEnabled(boneName: string, enabled: boolean)
	if State.activeRig and State.activeRig.bones then
		local rigBone = State.activeRig.bones[boneName]
		if rigBone then
			rigBone.enabled = enabled
			return
		end

		for _, rb in pairs(State.activeRig.bones) do
			if rb.part.Name == boneName then
				rb.enabled = enabled
				return
			end
		end
	end
end

local function refreshPlayback(services: any)
	if services and services.playbackService then
		services.playbackService:stopAnimationAndDisconnect()
		local kfsOverride = State.currentKeyframeSequence

		-- If we are replaying an existing sequence (e.g., a saved animation),
		-- update pose weights to honor current bone enabled states before playing.
		if kfsOverride then
			local rig = State.activeRig
			if rig and rig.bones then
				for _, keyframe in ipairs(kfsOverride:GetKeyframes()) do
					for _, pose in ipairs(keyframe:GetDescendants()) do
						if pose:IsA("Pose") then
							local rigBone = rig.bones[pose.Name]
							if rigBone then
								pose.Weight = rigBone.enabled and 1 or 0
							end
						end
					end
				end
			end
		end

		services.playbackService:playCurrentAnimation(State.activeAnimator, kfsOverride)
	end
end

local function applyBoneEnabled(
	boneWeights: { any },
	bone: any,
	enabled: boolean,
	services: any,
	skipRefresh: boolean?,
	skipStateUpdate: boolean?
)
	if bone.enabled == enabled then
		return false
	end

	bone.enabled = enabled
	if not skipStateUpdate then
		scheduleBoneWeightsUpdate(boneWeights)
	end
	syncRigBoneEnabled(bone.name, enabled)
	if not skipRefresh then
		refreshPlayback(services)
	end
	return true
end

function BoneToggles.create(services: any, layoutOrder: number?)
	local isDragging = Value(false)
	local dragTargetState = Value(nil :: boolean?)
	local releaseConn: RBXScriptConnection?
	local lastPaintedIndex = Value(nil :: number?)
	local dragChanged = false
	local dragOriginalWeights: { any }?
	local dragDiff = {}
	local dragDiffVersion = Value(0)
	local finalizeDrag: (() -> ())?

	local function bumpDiffVersion()
		dragDiffVersion:set(dragDiffVersion:get() + 1)
	end

	local function setDragOverride(index: number, target: boolean)
		local previous = dragDiff[index]
		if previous == target then
			return
		end

		dragDiff[index] = target
		dragChanged = true
		bumpDiffVersion()
	end

	local function applyIfDragging(boneWeights: { any }, bone: any, index: number)
		local target = dragTargetState:get()
		if isDragging:get() and target ~= nil then
			local last = lastPaintedIndex:get()
			if last ~= nil and last ~= index then
				-- Only paint bones between last and current (the delta), not the entire range
				if index > last then
					for i = last + 1, index do
						local targetBone = boneWeights[i]
						if targetBone then
							setDragOverride(i, target)
						end
					end
				else
					for i = last - 1, index, -1 do
						local targetBone = boneWeights[i]
						if targetBone then
							setDragOverride(i, target)
						end
					end
				end
			else
				setDragOverride(index, target)
			end
			lastPaintedIndex:set(index)
		end
	end

	local function ensureReleaseConnection()
		if releaseConn then
			return finalizeDrag
		end

		local function finalizeDragLocal()
			isDragging:set(false)
			dragTargetState:set(nil)
			lastPaintedIndex:set(nil)
			if dragChanged and next(dragDiff) then
				local updated = copyBoneWeights(State.boneWeights:get())
				for idx, enabled in pairs(dragDiff) do
					local targetBone = updated[idx]
					if targetBone and targetBone.enabled ~= enabled then
						targetBone.enabled = enabled
						syncRigBoneEnabled(targetBone.name, enabled)
					end
				end
				State.boneWeights:set(updated)
				refreshPlayback(services)
			end
			table.clear(dragDiff)
			dragOriginalWeights = nil
			bumpDiffVersion()
			dragChanged = false
		end

		releaseConn = UserInputService.InputEnded:Connect(function(input)
			if input.UserInputType == Enum.UserInputType.MouseButton1 then
				if not isDragging:get() then
					return
				end
				finalizeDragLocal()
			end
		end)

		finalizeDrag = finalizeDragLocal
		return finalizeDragLocal
	end

	local function beginDrag(boneWeights: { any }, bone: any, index: number)
		finalizeDrag = ensureReleaseConnection()
		dragChanged = false
		dragOriginalWeights = boneWeights
		table.clear(dragDiff)
		local target = not bone.enabled
		dragTargetState:set(target)
		isDragging:set(true)
		setDragOverride(index, target)
		lastPaintedIndex:set(index)

		-- Handle simple click without movement by finalizing when mouse is released on the row
		return finalizeDrag
	end

	return VerticalCollapsibleSection({
		Text = "Bone Toggles",
		Collapsed = false,
		LayoutOrder = layoutOrder or 2,
		Visible = State.activeRigExists,
		[Children] = Computed(function()
			local _ = dragDiffVersion:get()
			local boneWeights = if isDragging:get() and dragOriginalWeights then dragOriginalWeights else State.boneWeights:get()
			local items = {}

			for i, bone in ipairs(boneWeights) do
				local indentWidth = bone.depth * 10
				local iconData = getBoneIconData(bone.name)
				local override = if isDragging:get() then dragDiff[i] else nil
				local effectiveEnabled = if override ~= nil then override else bone.enabled
				local checkboxValue = Value(effectiveEnabled)
				checkboxValue:set(effectiveEnabled)

				table.insert(
					items,
					New("Frame")({
						Size = UDim2.new(1, 0, 0, 24),
						BackgroundTransparency = 1,
						BorderSizePixel = 0,
						LayoutOrder = i,
						[OnEvent("InputBegan")] = function(input)
							if input.UserInputType == Enum.UserInputType.MouseButton1 then
								beginDrag(boneWeights, bone, i)
							end
						end,
						[OnEvent("InputEnded")] = function(input)
							if input.UserInputType == Enum.UserInputType.MouseButton1 then
								if not isDragging:get() then
									return
								end
								if finalizeDrag then
									finalizeDrag()
								end
							end
						end,
						[OnEvent("MouseEnter")] = function()
							applyIfDragging(boneWeights, bone, i)
						end,
						[OnEvent("MouseMoved")] = function()
							applyIfDragging(boneWeights, bone, i)
						end,
						[Children] = {
							New("UICorner")({
								CornerRadius = UDim.new(0, 3),
							}),
							New("UIListLayout")({
								FillDirection = Enum.FillDirection.Horizontal,
								VerticalAlignment = Enum.VerticalAlignment.Center,
								Padding = UDim.new(0, 6),
								SortOrder = Enum.SortOrder.LayoutOrder,
							}),
							New("UIPadding")({
								PaddingLeft = UDim.new(0, 8),
								PaddingRight = UDim.new(0, 8),
								PaddingTop = UDim.new(0, 4),
								PaddingBottom = UDim.new(0, 4),
							}),
							New("Frame")({
								Size = UDim2.fromOffset(indentWidth, 1),
								BackgroundTransparency = 1,
								LayoutOrder = 1,
							}),
							Checkbox({
								Value = checkboxValue,
								Text = "",
								Size = UDim2.fromOffset(16, 16),
								LayoutOrder = 2,
								[OnEvent("InputBegan")] = function(input)
									if input.UserInputType == Enum.UserInputType.MouseButton1 then
										beginDrag(boneWeights, bone, i)
									end
								end,
								[OnEvent("MouseEnter")] = function()
									applyIfDragging(boneWeights, bone, i)
								end,
								[OnEvent("MouseMoved")] = function()
									applyIfDragging(boneWeights, bone, i)
								end,
								OnChange = function(enabled: boolean)
									if isDragging:get() then
										setDragOverride(i, enabled)
										dragTargetState:set(enabled)
									else
										applyBoneEnabled(boneWeights, bone, enabled, services, false, false)
									end
									lastPaintedIndex:set(i)
									checkboxValue:set(enabled)
								end,
							}),
							New("ImageLabel")({
								BackgroundTransparency = 1,
								Size = UDim2.fromOffset(18, 18),
								LayoutOrder = 3,
								Image = iconData and iconData.Image or "",
								ImageRectOffset = iconData and iconData.ImageRectOffset or Vector2.new(0, 0),
								ImageRectSize = iconData and iconData.ImageRectSize or Vector2.new(0, 0),
								ImageColor3 = Computed(function()
									dragDiffVersion:get()
									local overrideLocal = if isDragging:get() then dragDiff[i] else nil
									local effectiveEnabledLocal = if overrideLocal ~= nil then overrideLocal else bone.enabled
									local colorState = if effectiveEnabledLocal
										then themeProvider:GetColor(Enum.StudioStyleGuideColor.MainText)
										else themeProvider:GetColor(Enum.StudioStyleGuideColor.DimmedText)
									return colorState:get()
								end),
							}),
							New("Frame")({
								Size = UDim2.new(1, -80 - indentWidth, 1, 0),
								BackgroundTransparency = 1,
								LayoutOrder = 4,
								[Children] = {
									New("TextLabel")({
										BackgroundTransparency = 1,
										Size = UDim2.new(1, 0, 1, 0),
										TextXAlignment = Enum.TextXAlignment.Left,
										TextTruncate = Enum.TextTruncate.AtEnd,
										Text = bone.name,
										Font = themeProvider:GetFont("Default"),
										TextSize = constants.TextSize,
										TextColor3 = Computed(function()
											dragDiffVersion:get()
											local overrideLocal = if isDragging:get() then dragDiff[i] else nil
											local effectiveEnabledLocal = if overrideLocal ~= nil then overrideLocal else bone.enabled
											local colorState = if effectiveEnabledLocal
												then themeProvider:GetColor(Enum.StudioStyleGuideColor.MainText)
												else themeProvider:GetColor(Enum.StudioStyleGuideColor.DimmedText)
											return colorState:get()
										end),
									}),
								},
							}),
						},
					})
				)
			end

			return items
		end),
	})
end

return BoneToggles
