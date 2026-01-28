--!native
--!strict
--!optimize 2

local State = require(script.Parent.Parent.Parent.state)
local Types = require(script.Parent.Parent.Parent.types)
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
local Label = require(StudioComponents.Label)
local VerticalCollapsibleSection = require(StudioComponents.VerticalCollapsibleSection)

local StudioComponentsUtil = StudioComponents:FindFirstChild("Util")
local themeProvider = require(StudioComponentsUtil.themeProvider)
local constants = require(StudioComponentsUtil.constants)

local ExportBoneToggles = {}

local exportWeightsUpdateQueued = false
local function scheduleExportWeightsUpdate(boneWeights: Types.ExportBoneWeightsList)
	if exportWeightsUpdateQueued then
		return
	end
	exportWeightsUpdateQueued = true
	task.defer(function()
		exportWeightsUpdateQueued = false
		local cloned = table.clone(boneWeights)
		State.exportBoneWeights:set(cloned)
	end)
end

local function syncRigBoneExportEnabled(boneName: string, enabled: boolean)
	if State.activeRig and State.activeRig.bones then
		local rigBone = State.activeRig.bones[boneName]
		if rigBone then
			rigBone.exportEnabled = enabled
			return
		end

		for _, rb in pairs(State.activeRig.bones) do
			if rb.part.Name == boneName then
				rb.exportEnabled = enabled
				return
			end
		end
	end
end

local function getBoneIconData(boneName: string)
	local className = "Bone"
	if State.activeRig and State.activeRig.bones then
		local rigBone = State.activeRig.bones[boneName]
		if rigBone and rigBone.joint then
			if rigBone.joint:IsA("Motor6D") then
				className = "Motor6D"
			elseif rigBone.joint:IsA("AnimationConstraint") then
				className = "AnimationConstraint"
			else
				-- For other joint types (Weld, WeldConstraint), use Motor6D icon
				className = "Motor6D"
			end
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

local function applyExportEnabled(
	boneWeights: Types.ExportBoneWeightsList,
	bone: Types.ExportBoneWeight,
	enabled: boolean
)
	if bone.enabled == enabled then
		return false
	end

	bone.enabled = enabled
	scheduleExportWeightsUpdate(boneWeights)
	syncRigBoneExportEnabled(bone.name, enabled)
	return true
end

function ExportBoneToggles.create(services: any, layoutOrder: number?)
	local isDragging = Value(false)
	local dragTargetState = Value(nil :: boolean?)
	local releaseConn: RBXScriptConnection?
	local lastPaintedIndex = Value(nil :: number?)

	local function applyIfDragging(boneWeights: Types.ExportBoneWeightsList, bone: Types.ExportBoneWeight, index: number)
		local target = dragTargetState:get()
		if isDragging:get() and target ~= nil then
			local last = lastPaintedIndex:get()
			if last ~= nil and last ~= index then
				if index > last then
					for i = last + 1, index do
						local targetBone = boneWeights[i]
						if targetBone then
							applyExportEnabled(boneWeights, targetBone, target)
						end
					end
				else
					for i = last - 1, index, -1 do
						local targetBone = boneWeights[i]
						if targetBone then
							applyExportEnabled(boneWeights, targetBone, target)
						end
					end
				end
			else
				applyExportEnabled(boneWeights, bone, target)
			end
			lastPaintedIndex:set(index)
		end
	end

	local function ensureReleaseConnection()
		if releaseConn then
			return
		end
		releaseConn = UserInputService.InputEnded:Connect(function(input)
			if input.UserInputType == Enum.UserInputType.MouseButton1 then
				isDragging:set(false)
				dragTargetState:set(nil)
				lastPaintedIndex:set(nil)
			end
		end)
	end

	local function beginDrag(boneWeights: Types.ExportBoneWeightsList, bone: Types.ExportBoneWeight, index: number)
		ensureReleaseConnection()
		local target = not bone.enabled
		dragTargetState:set(target)
		isDragging:set(true)
		applyExportEnabled(boneWeights, bone, target)
		lastPaintedIndex:set(index)
	end

	return VerticalCollapsibleSection({
		Text = "Export Bone Toggles",
		Collapsed = false,
		LayoutOrder = layoutOrder or 2,
		Visible = State.activeRigExists,
		[Children] = Computed(function()
			local boneWeights: Types.ExportBoneWeightsList = State.exportBoneWeights:get()
			local items: { Instance } = {}
			local boneByName: { [string]: Types.ExportBoneWeight } = {}
			for _, bw in ipairs(boneWeights) do
				boneByName[bw.name] = bw
			end

			local function isBoneBlocked(bone: Types.ExportBoneWeight): boolean
				local parentName = bone.parentName
				while parentName do
					local parentBone = boneByName[parentName]
					if not parentBone then
						break
					end
					if parentBone.enabled == false then
						return true
					end
					parentName = parentBone.parentName
				end
				return false
			end

			table.insert(
				items,
				Label({
					Text = "Affects rig export only (does not change playback)",
					LayoutOrder = 0,
					TextWrapped = true,
				})
			)

			for i, bone in ipairs(boneWeights) do
				local indentWidth = bone.depth * 10
				local iconData = getBoneIconData(bone.name)
				local isBlocked = isBoneBlocked(bone)
				local isInteractive = not isBlocked
				local displayEnabled = bone.enabled and not isBlocked

				table.insert(
					items,
					New("Frame")({
						Size = UDim2.new(1, 0, 0, 24),
						BackgroundTransparency = 1,
						BorderSizePixel = 0,
						LayoutOrder = i,
						[OnEvent("InputBegan")] = function(input)
							if input.UserInputType == Enum.UserInputType.MouseButton1 and isInteractive then
								beginDrag(boneWeights, bone, i)
							end
						end,
						[OnEvent("InputEnded")] = function(input)
							if input.UserInputType == Enum.UserInputType.MouseButton1 then
								isDragging:set(false)
								dragTargetState:set(nil)
								lastPaintedIndex:set(nil)
							end
						end,
						[OnEvent("MouseEnter")] = function()
							if isInteractive then
								applyIfDragging(boneWeights, bone, i)
							end
						end,
						[OnEvent("MouseMoved")] = function()
							if isInteractive then
								applyIfDragging(boneWeights, bone, i)
							end
						end,
						[Children] = {
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
								Value = displayEnabled,
								Text = "",
								Size = UDim2.fromOffset(16, 16),
								LayoutOrder = 2,
								Enabled = isInteractive,
								[OnEvent("InputBegan")] = function(input)
									if input.UserInputType == Enum.UserInputType.MouseButton1 and isInteractive then
										beginDrag(boneWeights, bone, i)
									end
								end,
								[OnEvent("MouseEnter")] = function()
									if isInteractive then
										applyIfDragging(boneWeights, bone, i)
									end
								end,
								[OnEvent("MouseMoved")] = function()
									if isInteractive then
										applyIfDragging(boneWeights, bone, i)
									end
								end,
								OnChange = function(enabled: boolean)
									if isInteractive then
										applyExportEnabled(boneWeights, bone, enabled)
										lastPaintedIndex:set(i)
									end
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
									local colorState = if displayEnabled
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
											local colorState = if displayEnabled
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

return ExportBoneToggles