--!native
--!strict
--!optimize 2

local State = require(script.Parent.Parent.state)
local Types = require(script.Parent.Parent.types)
local Utils = require(script.Parent.Parent.Utils)

local AnimationSerializer = require(script.Parent.Parent.Components.AnimationSerializer)
local SelectionService = game:GetService("Selection")

local AnimationManager = {}
AnimationManager.__index = AnimationManager

-- Priority lookup table to avoid dynamic enum access
local priorityLookup = {
	Action = Enum.AnimationPriority.Action,
	Action2 = Enum.AnimationPriority.Action2,
	Action3 = Enum.AnimationPriority.Action3,
	Action4 = Enum.AnimationPriority.Action4,
	Core = Enum.AnimationPriority.Core,
	Idle = Enum.AnimationPriority.Idle,
	Movement = Enum.AnimationPriority.Movement,
}

local function ensureChannelSample(poseMap, poseName, keyTime)
	poseMap[poseName] = poseMap[poseName] or {}
	poseMap[poseName][keyTime] = poseMap[poseName][keyTime]
		or { Position = {}, Rotation = {} }
	return poseMap[poseName][keyTime]
end

local function createAxisTimeline()
	return {
		Position = { X = {}, Y = {}, Z = {} },
		Rotation = { X = {}, Y = {}, Z = {} },
	}
end

local function sampleAxisValue(series, timePosition)
	if not series or #series == 0 then
		return nil
	end

	local last = series[#series]
	if timePosition >= last.time then
		return last.value
	end

	for i = 1, #series do
		local entry = series[i]
		if math.abs(entry.time - timePosition) <= 1e-5 then
			return entry.value
		elseif entry.time > timePosition then
			if i == 1 then
				return entry.value
			end
			local prev = series[i - 1]
			local span = entry.time - prev.time
			if span <= 0 then
				return prev.value
			end
			local alpha = (timePosition - prev.time) / span
			return prev.value + (entry.value - prev.value) * alpha
		end
	end

	return series[#series].value
end

local function interpolateMissingAxis(finalValues, poseName, poseTime, axisTimelines)
	local poseTimeline = axisTimelines[poseName]

	local function fill(axisKind, axis)
		local prefix = axisKind == "Position" and "P" or "R"
		if finalValues[prefix .. axis] ~= nil then
			return
		end

		local axisSeries = poseTimeline and poseTimeline[axisKind]
			and poseTimeline[axisKind][axis]
		local sampled = sampleAxisValue(axisSeries, poseTime)
		finalValues[prefix .. axis] = sampled or 0
	end

	fill("Position", "X")
	fill("Position", "Y")
	fill("Position", "Z")
	fill("Rotation", "X")
	fill("Rotation", "Y")
	fill("Rotation", "Z")
end

local function applyPosesFromCurves(poseMap, namePosePairs, faceControlMap, keyTimes, axisTimelines)
	for _, poseTime in ipairs(keyTimes) do
		for poseName, poseTable in pairs(namePosePairs) do
			local pose = poseTable[poseTime]
			if not pose or pose:IsA("Folder") then
				continue
			end

			if pose:IsA("NumberPose") then
				local channel = faceControlMap[poseName]
				if channel and channel[poseTime] ~= nil then
					pose.Value = channel[poseTime]
					pose.Weight = 1
				end
				continue
			end

			local jointChannels = poseMap[poseName]
			if not jointChannels then
				continue
			end

			local channelSample = jointChannels[poseTime]
			if not channelSample then
				channelSample = { Position = {}, Rotation = {} }
			end

			local finalValues = {
				PX = channelSample.Position.X,
				PY = channelSample.Position.Y,
				PZ = channelSample.Position.Z,
				RX = channelSample.Rotation.X,
				RY = channelSample.Rotation.Y,
				RZ = channelSample.Rotation.Z,
			}

			interpolateMissingAxis(finalValues, poseName, poseTime, axisTimelines)
			pose.Weight = 1
			pose.CFrame = CFrame.new(
				finalValues.PX or 0,
				finalValues.PY or 0,
				finalValues.PZ or 0
			) * CFrame.Angles(
				finalValues.RX or 0,
				finalValues.RY or 0,
				finalValues.RZ or 0
			)
		end
	end
end

local function mapCurveChannels(curveAnimation)
	local poseMap = {}
	local faceControlMap = {}
	local keyTimesSet = {}
	local axisTimelines = {}
	local processedCount = 0

	local function registerTime(time)
		keyTimesSet[time] = true
	end

	local function recordAxisSample(poseName, axisKind, axis, key)
		registerTime(key.Time)
		local channelSample = ensureChannelSample(poseMap, poseName, key.Time)
		channelSample[axisKind][axis] = key.Value

		axisTimelines[poseName] = axisTimelines[poseName] or createAxisTimeline()
		local series = axisTimelines[poseName][axisKind][axis]
		series[#series + 1] = { time = key.Time, value = key.Value }
	end

	for _, curve in ipairs(curveAnimation:GetDescendants()) do
		processedCount = processedCount + 1
		if processedCount % 50 == 0 then
			task.wait()
		end

		if curve:IsA("Vector3Curve") then
			local poseName = curve.Parent.Name
			for _, key in ipairs(curve:X():GetKeys()) do
				recordAxisSample(poseName, "Position", "X", key)
			end
			for _, key in ipairs(curve:Y():GetKeys()) do
				recordAxisSample(poseName, "Position", "Y", key)
			end
			for _, key in ipairs(curve:Z():GetKeys()) do
				recordAxisSample(poseName, "Position", "Z", key)
			end
		elseif curve:IsA("EulerRotationCurve") then
			local poseName = curve.Parent.Name
			for _, key in ipairs(curve:X():GetKeys()) do
				recordAxisSample(poseName, "Rotation", "X", key)
			end
			for _, key in ipairs(curve:Y():GetKeys()) do
				recordAxisSample(poseName, "Rotation", "Y", key)
			end
			for _, key in ipairs(curve:Z():GetKeys()) do
				recordAxisSample(poseName, "Rotation", "Z", key)
			end
		elseif curve:IsA("FloatCurve") and curve.Parent and curve.Parent.Name == "FaceControls" then
			local controlName = curve.Name
			faceControlMap[controlName] = faceControlMap[controlName] or {}
			for _, key in ipairs(curve:GetKeys()) do
				registerTime(key.Time)
				faceControlMap[controlName][key.Time] = key.Value
			end
		end
	end

	-- ensure each axis timeline is time-sorted for interpolation
	for _, poseTimeline in pairs(axisTimelines) do
		for _, axisKind in pairs(poseTimeline) do
			for axisName, series in pairs(axisKind) do
				table.sort(series, function(a, b)
					return a.time < b.time
				end)
				axisKind[axisName] = series
			end
		end
	end

	local keyTimes = {}
	for time in pairs(keyTimesSet) do
		table.insert(keyTimes, time)
	end
	table.sort(keyTimes)

	return poseMap, keyTimes, faceControlMap, axisTimelines
end

local function createEmptyKeyframes(sequence, curveAnimation, keyTimes)
	local keyframeTimePairs = {}
	local namePosePairs = {}
	local keyframeCount = 0

	for _, keyTime in ipairs(keyTimes) do
		keyframeCount = keyframeCount + 1
		if keyframeCount % 100 == 0 then
			task.wait()
		end

		local keyframe = Instance.new("Keyframe")
		keyframe.Time = keyTime
		keyframe.Parent = sequence
		keyframeTimePairs[keyTime] = { keyTime, keyframe }
	end

	local function addChild(keyPair, node, parentPose)
		local isFaceFloat = node.Parent and node.Parent.Name == "FaceControls" and node:IsA("FloatCurve")
		if not (node:IsA("Folder") or isFaceFloat) then
			return
		end

		local pose
		if node.Name == "FaceControls" then
			pose = Instance.new("Folder")
		elseif isFaceFloat then
			pose = Instance.new("NumberPose")
		else
			pose = Instance.new("Pose")
		end

		pose.Name = node.Name
		if pose:IsA("Pose") then
			pose.CFrame = CFrame.new()
			pose.Weight = 0
		elseif pose:IsA("NumberPose") then
			pose.Value = 0
			pose.Weight = 0
		end
		pose.Parent = parentPose

		namePosePairs[node.Name] = namePosePairs[node.Name] or {}
		namePosePairs[node.Name][keyPair[1]] = pose

		for _, child in ipairs(node:GetChildren()) do
			addChild(keyPair, child, pose)
		end
	end

	local pairCount = 0
	for _, pair in pairs(keyframeTimePairs) do
		pairCount = pairCount + 1
		if pairCount % 50 == 0 then
			task.wait()
		end

		for _, child in ipairs(curveAnimation:GetChildren()) do
			addChild(pair, child, pair[2])
		end
	end

	return namePosePairs, keyframeTimePairs
end

local function applyMarkersFromCurves(sequence, curveAnimation, keyframeTimePairs)
	local markersByTime = {}
	local markerCount = 0

	for _, markerCurve in ipairs(curveAnimation:GetDescendants()) do
		if not markerCurve:IsA("MarkerCurve") then
			continue
		end
		for _, markerInfo in ipairs(markerCurve:GetMarkers()) do
			markerCount = markerCount + 1
			if markerCount % 100 == 0 then
				task.wait()
			end

			markersByTime[markerInfo.Time] = markersByTime[markerInfo.Time] or {}
			table.insert(markersByTime[markerInfo.Time], { markerCurve.Name, markerInfo.Value })
		end
	end

	for markerTime, markers in pairs(markersByTime) do
		local keyframePair = keyframeTimePairs[markerTime]
		local keyframe
		if keyframePair then
			keyframe = keyframePair[2]
		else
			keyframe = Instance.new("Keyframe")
			keyframe.Time = markerTime
			keyframe.Parent = sequence
		end

		for _, markerInfo in ipairs(markers) do
			local marker = Instance.new("KeyframeMarker")
			marker.Name = markerInfo[1]
			marker.Value = markerInfo[2]
			marker.Parent = keyframe
		end
	end
end

local function curveAnimationToKeyframeSequence(curveAnimation)
	local poseMap, keyTimes, faceControlMap, axisTimelines = mapCurveChannels(curveAnimation)
	if #keyTimes == 0 then
		return nil
	end

	local sequence = Instance.new("KeyframeSequence")
	sequence.Name = curveAnimation.Name
	sequence.Loop = curveAnimation.Loop
	sequence.Priority = curveAnimation.Priority

	local namePosePairs, keyframeTimePairs = createEmptyKeyframes(sequence, curveAnimation, keyTimes)
	applyPosesFromCurves(poseMap, namePosePairs, faceControlMap, keyTimes, axisTimelines)
	applyMarkersFromCurves(sequence, curveAnimation, keyframeTimePairs)

	return sequence
end

function AnimationManager.new(playbackService: any, pluginObj: Plugin?)
	local self = setmetatable({}, AnimationManager)
	
	self.playbackService = playbackService
	self.plugin = pluginObj
	self.animationSerializerService = AnimationSerializer.new()
	
	return self
end

function AnimationManager:displayDetailedError(title, message)
	warn(title, message)
end

function AnimationManager:loadAnim(data: string, isBinary: boolean)
	local animData = self.animationSerializerService:deserialize(data, isBinary)

	if not animData then
		error("Failed to deserialize animation data.")
	end

	-- Load the animation
	local _loadSuccess, loadError = pcall(function()
		assert(State.activeRig, "activeRig is nil")
		State.activeRig:LoadAnimation(animData)
		return true
	end)

	if not _loadSuccess then
		error("Animation loading failed: " .. tostring(loadError))
	end
end

function AnimationManager:loadAnimDataFromText(text: string, isBinary: boolean)
	local ok = pcall(self.loadAnim, self, text, isBinary)
	if ok then
		local success, result = pcall(self.loadRig, self)
		if success then
			print("Animation loaded successfully.")
			return true
		else
			self:displayDetailedError("Error during rig loading", tostring(result))
			return false
		end
	else
		self:displayDetailedError("Error during animation data loading", "Unknown error")
		return false
	end
end

-- Legacy-friendly loader: try binary first, then base64 text fallback.
function AnimationManager:loadAnimDataAuto(text: string)
    if self:loadAnimDataFromText(text, true) then
        return true
    end
    return self:loadAnimDataFromText(text, false)
end

function AnimationManager:loadRig(animationToLoad: KeyframeSequence?)
	self.playbackService:stopAnimationAndDisconnect()

	if not State.activeRig then
		error("No active rig available")
	end

	local kfs: KeyframeSequence
	if animationToLoad then
		kfs = animationToLoad:Clone()
	else
		kfs = State.activeRig:ToRobloxAnimation()
	end

	if State.scaleFactor:get() ~= 1 then
		kfs = Utils.scaleAnimation(kfs, State.scaleFactor:get()) -- Scale the animation
	end

	-- Ensure the rig holds the loaded animation data so saving back to rig works (even for CurveAnimation-derived clips)
	pcall(function()
		State.activeRig:LoadAnimation(kfs)
	end)

	-- Detect torso animation data on R6 rigs
	local function hasTorsoMotion(seq: KeyframeSequence): boolean
		local ok, result = pcall(function()
			for _, keyframe in ipairs(seq:GetKeyframes()) do
				for _, pose in ipairs(keyframe:GetDescendants()) do
					if pose:IsA("Pose") and pose.Name == "Torso" then
						if pose.Weight > 0 then
							local cf = pose.CFrame
							if cf then
								local ox, oy, oz = cf:ToOrientation()
								if cf.Position.Magnitude > 1e-4 or math.abs(ox) > 1e-4 or math.abs(oy) > 1e-4 or math.abs(oz) > 1e-4 then
									return true
								end
							end
						end
					end
				end
			end
			return false
		end)
		if not ok then
			warn("Torso motion check failed:", result)
			return false
		end
		return result
	end

	local rigModel = State.activeRigModel
	local humanoid = rigModel and rigModel:FindFirstChildOfClass("Humanoid")
	local hasTorsoData = humanoid and humanoid.RigType == Enum.HumanoidRigType.R6 and hasTorsoMotion(kfs)

	-- Apply current bone toggle weights after any scaling so the final sequence honors UI toggles
	local function applyBoneWeights(sequence: KeyframeSequence)
		local rig = State.activeRig
		if not rig or not rig.bones then
			return
		end

		for _, keyframe in ipairs(sequence:GetKeyframes()) do
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

	applyBoneWeights(kfs)

	State.animationData = (kfs:GetKeyframes() :: any) :: { Types.KeyframeType }?
	State.animationLength:set(Utils.getAnimDuration(State.animationData))
	
	-- Yield before playing large animations to prevent freezing
	local keyframes = (kfs:GetKeyframes() :: any) :: { Types.KeyframeType }?
	if keyframes and #keyframes > 500 then
		task.wait()
	end
	
	self.playbackService:playCurrentAnimation(State.activeAnimator, kfs)

	-- If torso has animation data on R6, verify the torso part actually moves; otherwise warn about Adaptive Animations beta
	if hasTorsoData and rigModel then
		task.spawn(function()
			local torso: BasePart?
			do
				local direct = rigModel:FindFirstChild("Torso")
				if direct and direct:IsA("BasePart") then
					torso = direct
				else
					for _, inst in ipairs(rigModel:GetDescendants()) do
						if inst:IsA("BasePart") and inst.Name == "Torso" then
							torso = inst
							break
						end
					end
				end
			end

			if not torso then
				return
			end

			-- If torso is anchored, don't warn; lack of movement is expected.
			if torso.Anchored then
				return
			end

			local start = torso.CFrame
			task.wait(0.35)
			local current = torso.CFrame
			local delta = start:ToObjectSpace(current)
			local posDelta = delta.Position.Magnitude
			local rx, ry, rz = delta:ToOrientation()
			local rotDelta = math.abs(rx) + math.abs(ry) + math.abs(rz)

			if posDelta < 1e-3 and rotDelta < 1e-3 then
				if State.rigManager and State.rigManager.addWarning then
					State.rigManager:addWarning(
						"Torso has animation data but is not moving. Disable the Adaptive Animations beta feature in Studio, it completely breaks R6. File > Beta Features > Adaptive Animations (uncheck this)"
					)
				end
			end
		end)
	end

	-- Calculate keyframe statistics
	local count = keyframes and #keyframes or 0
	local totalDuration = keyframes and Utils.getAnimDuration(keyframes) or 0

	State.keyframeStats:set({
		count = count,
		totalDuration = totalDuration,
	})
	return true
end

function AnimationManager:createKeyframeSequenceFromState(): KeyframeSequence?
	if not State.activeRig then
		return nil
	end

	State.activeRig.keyframeNames = State.keyframeNames:get() :: { any }?
	local kfs = State.activeRig:ToRobloxAnimation()

	if State.scaleFactor:get() ~= 1 then
		kfs = Utils.scaleAnimation(kfs, State.scaleFactor:get())
	end

	kfs.Loop = State.loopAnimation:get()
	kfs.Priority = priorityLookup[State.selectedPriority:get()]

	if State.animationName and State.animationName ~= "" then
		kfs.Name = State.animationName
	else
		kfs.Name = "KeyframeSequence"
	end

	return kfs
end

function AnimationManager:saveAnimationRig()
	if not State.activeRigModel then
		warn("No active rig model set.")
		return
	end

	-- Prefer the currently loaded/playing sequence (from saved or curve animations) to avoid blank exports.
	local kfs
	if State.currentKeyframeSequence then
		kfs = State.currentKeyframeSequence:Clone()
	else
		kfs = self:createKeyframeSequenceFromState()
	end
	if not kfs then
		return
	end

	if State.animationName and State.animationName ~= "" then
		kfs.Name = State.animationName
	else
		kfs.Name = "KeyframeSequence"
	end

	local animSaves: any = State.activeRigModel:FindFirstChild("AnimSaves")

	if not animSaves then
		animSaves = Instance.new("ObjectValue")
		animSaves.Name = "AnimSaves"
		animSaves.Value = nil -- ObjectValue must point to an object, but we'll use this as a container
		animSaves.Parent = State.activeRigModel
	end

	if State.uniqueNames:get() then
		local animSavesDescendants = animSaves:GetDescendants()
		local existingNames = {}
		for _, descendant in ipairs(animSavesDescendants) do
			existingNames[descendant.Name] = true
		end

		local baseName = kfs.Name
		local finalName = baseName
		if existingNames[baseName] then
			local i = 1
			while true do
				finalName = baseName .. "_" .. tostring(i)
				if not existingNames[finalName] then
					break
				end
				i = i + 1
			end
			kfs.Name = finalName
		end
	end

	kfs.Parent = animSaves

	if State.rigManager and State.rigManager.updateSavedAnimationsList then
		State.rigManager:updateSavedAnimationsList()
	end
end

function AnimationManager:saveAnimationFolder(name: string)
	if not State.activeRigModel then
		warn("No active rig model set.")
		return
	end

	local folder = game.Workspace:FindFirstChild("Imported Animations Folder")

	if not folder then
		folder = Instance.new("Folder")
		folder.Name = "Imported Animations Folder"
		folder.Parent = game.Workspace
	end
	assert(State.activeRig)
	local kfs = State.activeRig:ToRobloxAnimation()
	if State.scaleFactor:get() ~= 1 then
		kfs = Utils.scaleAnimation(kfs, State.scaleFactor:get()) -- Scale the animation
	end

	if name then
		kfs.Name = name
	end

	kfs.Parent = folder

	kfs.Priority = priorityLookup[State.selectedPriority:get()]
end

function AnimationManager:uploadAnimation()
	if not State.activeRigModel then
		warn("No active rig set for uploading animation.")
		return
	end

	if not self.plugin then
		warn("Plugin reference missing; cannot upload animation.")
		return
	end

	local kfs = self:createKeyframeSequenceFromState()
	if not kfs then
		return
	end
    kfs.Parent = game.Workspace

    -- upload the selected KeyframeSequence
    SelectionService:Set({ kfs })
    self.plugin:SaveSelectedToRoblox()

    -- persist the uploaded sequence instead of deleting it
    -- move it under the active rig's AnimSaves container for user access
    local animSaves: any = State.activeRigModel:FindFirstChild("AnimSaves")
    if not animSaves then
        animSaves = Instance.new("ObjectValue")
        animSaves.Name = "AnimSaves"
        animSaves.Parent = State.activeRigModel
    end

    -- ensure unique name if needed
    if State.uniqueNames:get() then
        local existingNames = {}
        for _, d in ipairs(animSaves:GetDescendants()) do
            existingNames[d.Name] = true
        end
        local baseName = kfs.Name
        local finalName = baseName
        if existingNames[baseName] then
            local i = 1
            while true do
                finalName = baseName .. "_" .. tostring(i)
                if not existingNames[finalName] then
                    break
                end
                i += 1
            end
        end
        kfs.Name = finalName
    end

    kfs.Parent = animSaves

	if State.rigManager and State.rigManager.updateSavedAnimationsList then
		State.rigManager:updateSavedAnimationsList()
	end
end

function AnimationManager:playSavedAnimation(animation)
	if not animation or not (animation :: any).instance then
		return
	end

	local instance = (animation :: any).instance
	local keyframeSequence
	if instance:IsA("KeyframeSequence") then
		keyframeSequence = instance
	elseif instance:IsA("CurveAnimation") then
		keyframeSequence = curveAnimationToKeyframeSequence(instance)
		if not keyframeSequence then
			warn("Failed to convert CurveAnimation '" .. instance.Name .. "' to KeyframeSequence")
			return
		end
	else
		warn("Unsupported animation type:", instance.ClassName)
		return
	end

	self:loadRig(keyframeSequence)
end

function AnimationManager:importAnimationsBulk()
	if State.activeRig then
		self.playbackService:stopAnimationAndDisconnect({ background = true })

		local animfiles = game:GetService("StudioService"):PromptImportFiles({ "rbxanim" })

		if animfiles then
			if #animfiles > 1 then
                for _, animfile in ipairs(animfiles) do
					self.playbackService:stopAnimationAndDisconnect({ background = true })

					local loaded = (animfile :: any):GetBinaryContents()
                    local success = pcall(function()
                        self:loadAnimDataAuto(loaded)
                    end)
					if success then
						local name = string.gsub(animfile.Name, ".rbxanim", "")
						self:saveAnimationFolder(name)
					else
						warn("Error loading animation")
					end
				end
			else
                for _, animfile in ipairs(animfiles) do
					self.playbackService:stopAnimationAndDisconnect({ background = true })

					local loaded = (animfile :: any):GetBinaryContents()
                    pcall(function()
                        self:loadAnimDataAuto(loaded)
                    end)
				end
			end
		else
			-- Handle the case where no files were selected or the operation was canceled.
			print("No files were imported.")
		end
	else
		warn("No active rig set for bulk importing animations.")
	end
end

function AnimationManager:importAnimationsFromRoblox()
	if not State.activeRig then
		warn("No active rig set for Roblox import.")
		return
	end

	local provider = game:GetService("AnimationClipProvider")
	if not self.plugin then
		warn("Plugin reference missing; cannot open Roblox import dialog.")
		return
	end

	local selectionId = self.plugin:PromptForExistingAssetId("Animation")
	if not selectionId or selectionId == "" then
		return
	end

	local contentId = tostring(selectionId)
	if not string.find(contentId, "://") then
		contentId = "rbxassetid://" .. contentId
	end

	local success, clipOrErr = pcall(function()
		return provider:GetAnimationClipAsync(contentId)
	end)

	if not success then
		warn("Failed to load animation clip:", clipOrErr)
		return
	end

	local clip = clipOrErr
	if not clip then
		return
	end

	local sequence
	if clip:IsA("KeyframeSequence") then
		sequence = clip
	elseif clip:IsA("CurveAnimation") then
		sequence = curveAnimationToKeyframeSequence(clip)
	else
		warn("Unsupported clip type:", clip.ClassName)
		return
	end

	local animSaves: any = State.activeRigModel and State.activeRigModel:FindFirstChild("AnimSaves")
	if not animSaves then
		animSaves = Instance.new("ObjectValue")
		animSaves.Name = "AnimSaves"
		animSaves.Parent = State.activeRigModel
	end

	sequence.Name = clip.Name
	sequence.Parent = animSaves
	if State.rigManager and State.rigManager.updateSavedAnimationsList then
		State.rigManager:updateSavedAnimationsList()
	else
		warn("RigManager missing; saved animation list may be stale.")
	end

	State.selectedSavedAnim:set({ name = sequence.Name, instance = sequence })
	self:loadRig(sequence)
end

function AnimationManager:addKeyframeName()
	local currentKeyframes = State.keyframeNames:get()
	table.insert(currentKeyframes, { name = State.keyframeNameInput:get(), time = State.playhead:get() })
	table.sort(currentKeyframes, function(a, b)
		return a.time < b.time
	end) -- Sort keyframes by time

	State.keyframeNames:set(currentKeyframes)
	State.keyframeNameInput:set("Name") -- Reset input field
end

function AnimationManager:removeKeyframeName(index)
	local currentKeyframes = State.keyframeNames:get()
	table.remove(currentKeyframes, index)
	State.keyframeNames:set(currentKeyframes)
end

return AnimationManager



