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

-- Reverse lookup: Enum -> string name
local priorityReverseLookup = {}
for name, enum in pairs(priorityLookup) do
	priorityReverseLookup[enum] = name
end

type AxisSample = { time: number, value: number }
type AxisSeries = { AxisSample }
type AxisTimeline = {
	Position: { X: AxisSeries, Y: AxisSeries, Z: AxisSeries },
	Rotation: { X: AxisSeries, Y: AxisSeries, Z: AxisSeries },
}

type ChannelSample = {
	Position: { X: number?, Y: number?, Z: number? },
	Rotation: { X: number?, Y: number?, Z: number? },
}

type PoseMap = { [string]: { [number]: ChannelSample } }
type FaceControlMap = { [string]: { [number]: number } }
type NamePosePairs = { [string]: { [number]: Instance } }
type KeyframePair = { time: number, keyframe: Keyframe }
type KeyframeTimePairs = { [number]: KeyframePair }
type CurveKey = { Time: number, Value: number }
type MarkerPair = { name: string, value: string }
type LoadingProgressContext = {
	set: (self: LoadingProgressContext, progress: number, status: string?, detail: string?, canEstimate: boolean?) -> (),
	child: (self: LoadingProgressContext, startProgress: number, endProgress: number) -> LoadingProgressContext,
}
type LoadOptions = {
	progress: LoadingProgressContext?,
	title: string?,
	detail: string?,
	suppressErrors: boolean?,
}

local function clamp01(value: number): number
	return math.max(0, math.min(1, value))
end

local function createLoadingProgressContext(manager: any, startProgress: number, endProgress: number): LoadingProgressContext
	local contextStart = startProgress
	local contextEnd = endProgress
	local context = {}

	function context:set(progress: number, status: string?, detail: string?, canEstimate: boolean?)
		local alpha = clamp01(progress)
		manager:_setLoadingProgress(
			contextStart + (contextEnd - contextStart) * alpha,
			status,
			detail,
			canEstimate
		)
	end

	function context:child(childStart: number, childEnd: number): LoadingProgressContext
		local range = contextEnd - contextStart
		return createLoadingProgressContext(
			manager,
			contextStart + range * clamp01(childStart),
			contextStart + range * clamp01(childEnd)
		)
	end

	return context :: any
end

local function ensureChannelSample(poseMap: PoseMap, poseName: string, keyTime: number): ChannelSample
	poseMap[poseName] = poseMap[poseName] or {}
	poseMap[poseName][keyTime] = poseMap[poseName][keyTime]
		or {
			Position = { X = nil, Y = nil, Z = nil },
			Rotation = { X = nil, Y = nil, Z = nil },
		}
	return poseMap[poseName][keyTime]
end

local function createAxisTimeline(): AxisTimeline
	return {
		Position = { X = {}, Y = {}, Z = {} },
		Rotation = { X = {}, Y = {}, Z = {} },
	}
end

local function sampleAxisValue(series: AxisSeries?, timePosition: number): number?
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

local function interpolateMissingAxis(
	finalValues: { [string]: number? },
	poseName: string,
	poseTime: number,
	axisTimelines: { [string]: AxisTimeline }
)
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

local function applyPosesFromCurves(
	poseMap: PoseMap,
	namePosePairs: NamePosePairs,
	faceControlMap: FaceControlMap,
	keyTimes: { number },
	axisTimelines: { [string]: AxisTimeline }
)
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
				channelSample = {
					Position = { X = nil, Y = nil, Z = nil },
					Rotation = { X = nil, Y = nil, Z = nil },
				}
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
			local poseInstance = pose :: Pose
			poseInstance.Weight = 1
			poseInstance.CFrame = CFrame.new(
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

local function mapCurveChannels(curveAnimation: CurveAnimation): (PoseMap, { number }, FaceControlMap, { [string]: AxisTimeline })
	local poseMap: PoseMap = {}
	local faceControlMap: FaceControlMap = {}
	local keyTimesSet: { [number]: boolean } = {}
	local axisTimelines: { [string]: AxisTimeline } = {}
	local processedCount = 0

	local function registerTime(time: number)
		keyTimesSet[time] = true
	end

	local function recordAxisSample(poseName: string, axisKind: string, axis: string, key: CurveKey)
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
			local curveParent = curve.Parent
			if not curveParent then
				continue
			end
			local poseName = curveParent.Name
			for _, key in ipairs(curve:X():GetKeys()) do
				recordAxisSample(poseName, "Position", "X", key :: CurveKey)
			end
			for _, key in ipairs(curve:Y():GetKeys()) do
				recordAxisSample(poseName, "Position", "Y", key :: CurveKey)
			end
			for _, key in ipairs(curve:Z():GetKeys()) do
				recordAxisSample(poseName, "Position", "Z", key :: CurveKey)
			end
		elseif curve:IsA("EulerRotationCurve") then
			local curveParent = curve.Parent
			if not curveParent then
				continue
			end
			local poseName = curveParent.Name
			for _, key in ipairs(curve:X():GetKeys()) do
				recordAxisSample(poseName, "Rotation", "X", key :: CurveKey)
			end
			for _, key in ipairs(curve:Y():GetKeys()) do
				recordAxisSample(poseName, "Rotation", "Y", key :: CurveKey)
			end
			for _, key in ipairs(curve:Z():GetKeys()) do
				recordAxisSample(poseName, "Rotation", "Z", key :: CurveKey)
			end
		elseif curve:IsA("FloatCurve") and curve.Parent and curve.Parent.Name == "FaceControls" then
			local controlName = curve.Name
			faceControlMap[controlName] = faceControlMap[controlName] or {}
			for _, key in ipairs(curve:GetKeys()) do
				local curveKey = key :: CurveKey
				registerTime(curveKey.Time)
				faceControlMap[controlName][curveKey.Time] = curveKey.Value
			end
		end
	end

	-- ensure each axis timeline is time-sorted for interpolation
	for _, poseTimeline in pairs(axisTimelines) do
		for _, axisKind in pairs(poseTimeline) do
			for axisName, series in pairs(axisKind) do
				local axisSeries = series :: AxisSeries
				table.sort(axisSeries, function(a: AxisSample, b: AxisSample)
					return a.time < b.time
				end)
				axisKind[axisName] = axisSeries
			end
		end
	end

	local keyTimes: { number } = {}
	for time in pairs(keyTimesSet) do
		table.insert(keyTimes, time)
	end
	table.sort(keyTimes)

	return poseMap, keyTimes, faceControlMap, axisTimelines
end

local function createEmptyKeyframes(
	sequence: KeyframeSequence,
	curveAnimation: CurveAnimation,
	keyTimes: { number }
): (NamePosePairs, KeyframeTimePairs)
	local keyframeTimePairs: KeyframeTimePairs = {}
	local namePosePairs: NamePosePairs = {}
	local keyframeCount = 0

	for _, keyTime in ipairs(keyTimes) do
		keyframeCount = keyframeCount + 1
		if keyframeCount % 100 == 0 then
			task.wait()
		end

		local keyframe = Instance.new("Keyframe")
		keyframe.Time = keyTime
		keyframe.Parent = sequence
		keyframeTimePairs[keyTime] = { time = keyTime, keyframe = keyframe }
	end

	local function addChild(keyPair: KeyframePair, node: Instance, parentPose: Instance)
		local isFaceFloat = node.Parent and node.Parent.Name == "FaceControls" and node:IsA("FloatCurve")
		if not (node:IsA("Folder") or isFaceFloat) then
			return
		end

		local pose: Instance
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
		namePosePairs[node.Name][keyPair.time] = pose

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
			addChild(pair, child, pair.keyframe)
		end
	end

	return namePosePairs, keyframeTimePairs
end

local function applyMarkersFromCurves(
	sequence: KeyframeSequence,
	curveAnimation: CurveAnimation,
	keyframeTimePairs: KeyframeTimePairs
)
	local markersByTime: { [number]: { MarkerPair } } = {}
	local markerCount = 0

	for _, markerCurve in ipairs(curveAnimation:GetDescendants()) do
		if not markerCurve:IsA("MarkerCurve") then
			continue
		end
		local typedCurve = markerCurve :: MarkerCurve
		for _, markerInfo in ipairs(typedCurve:GetMarkers()) do
			markerCount = markerCount + 1
			if markerCount % 100 == 0 then
				task.wait()
			end

			local markerTime = (markerInfo :: any).Time :: number
			local markerValue = (markerInfo :: any).Value :: string
			markersByTime[markerTime] = markersByTime[markerTime] or {}
			table.insert(markersByTime[markerTime], { name = markerCurve.Name, value = markerValue })
		end
	end

	for markerTime, markers in pairs(markersByTime) do
		local keyframePair = keyframeTimePairs[markerTime]
		local keyframe: Keyframe
		if keyframePair then
			keyframe = keyframePair.keyframe
		else
			keyframe = Instance.new("Keyframe")
			keyframe.Time = markerTime
			keyframe.Parent = sequence
		end

		for _, markerInfo in ipairs(markers) do
			local marker = Instance.new("KeyframeMarker")
			marker.Name = markerInfo.name
			marker.Value = markerInfo.value
			marker.Parent = keyframe
		end
	end
end

local function curveAnimationToKeyframeSequence(curveAnimation: CurveAnimation): KeyframeSequence?
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

function AnimationManager:_beginLoadingSession(title: string, status: string?, detail: string?)
	State.loadingEnabled:set(true)
	State.loadingTitle:set(title)
	State.loadingStatus:set(status or "Please wait...")
	State.loadingDetail:set(detail or "")
	State.loadingProgress:set(0)
	State.loadingCanEstimate:set(false)
end

function AnimationManager:_setLoadingProgress(progress: number, status: string?, detail: string?, canEstimate: boolean?)
	State.loadingEnabled:set(true)
	State.loadingProgress:set(clamp01(progress))
	if status ~= nil then
		State.loadingStatus:set(status)
	end
	if detail ~= nil then
		State.loadingDetail:set(detail)
	end
	if canEstimate ~= nil then
		State.loadingCanEstimate:set(canEstimate)
	end
end

function AnimationManager:_endLoadingSession()
	State.loadingEnabled:set(false)
	State.loadingTitle:set("Working")
	State.loadingStatus:set("Please wait...")
	State.loadingDetail:set("")
	State.loadingProgress:set(0)
	State.loadingCanEstimate:set(false)
end

function AnimationManager:_getLoadingContext(options: LoadOptions?, defaultTitle: string, defaultStatus: string): (LoadingProgressContext, boolean)
	if options and options.progress then
		return options.progress, false
	end

	self:_beginLoadingSession(defaultTitle, defaultStatus, if options then options.detail else "")
	return createLoadingProgressContext(self, 0, 1), true
end

function AnimationManager:displayDetailedError(title, message)
	warn(title, message)
end

function AnimationManager:loadAnim(data: string, isBinary: boolean, progressContext: LoadingProgressContext?)
	local decodeProgress = if progressContext then progressContext:child(0, 0.985) else nil
	local applyProgress = if progressContext then progressContext:child(0.985, 1) else nil
	local animData = self.animationSerializerService:deserialize(
		data,
		isBinary,
		if decodeProgress
			then function(progress: number, status: string?, detail: string?, canEstimate: boolean?)
				decodeProgress:set(progress, status, detail, canEstimate)
			end
			else nil
	)

	if not animData then
		error("Failed to deserialize animation data.")
	end

	if progressContext then
		progressContext:set(
			0.985,
			"Applying animation to rig",
			string.format("%d keyframes", if type(animData.kfs) == "table" then #animData.kfs else 0),
			true
		)
	end

	if type(animData.kfs) == "table" and #animData.kfs >= 200 then
		task.wait()
	end

	-- Load the animation
	local _loadSuccess, loadError = pcall(function()
		assert(State.activeRig, "activeRig is nil")
		local activeRig = State.activeRig :: any
		activeRig:LoadAnimation(
			animData,
			if applyProgress
				then function(progress: number, status: string?, detail: string?, canEstimate: boolean?)
					applyProgress:set(progress, status, detail, canEstimate)
				end
				else nil
		)
		return true
	end)

	if not _loadSuccess then
		error("Animation loading failed: " .. tostring(loadError))
	end

	if progressContext then
		progressContext:set(1, "Animation data applied", "building preview animation", true)
	end

	return animData
end

function AnimationManager:loadAnimDataFromText(text: string, isBinary: boolean, options: LoadOptions?)
	local progressContext, ownsSession = self:_getLoadingContext(options, "Importing Animation", if isBinary then "Reading binary animation" else "Reading text animation")
	local suppressErrors = if options then options.suppressErrors == true else false
	progressContext:set(0, if isBinary then "Reading binary animation" else "Reading text animation", if options then options.detail else "", false)

	local ok, result = pcall(self.loadAnim, self, text, isBinary, progressContext:child(0, 0.95))
	if ok then
		local success, rigResult = pcall(self.loadRig, self, nil, progressContext:child(0.95, 1))
		if success and rigResult ~= false then
			progressContext:set(
				1,
				"Animation loaded",
				string.format("%d keyframes ready", if type((result :: any).kfs) == "table" then #((result :: any).kfs) else 0),
				true
			)
			print("Animation loaded successfully.")
			if ownsSession then
				task.wait()
				self:_endLoadingSession()
			end
			return true
		else
			if not suppressErrors then
				self:displayDetailedError("Error during rig loading", tostring(rigResult))
			end
			if ownsSession then
				self:_endLoadingSession()
			end
			return false
		end
	else
		if not suppressErrors then
			self:displayDetailedError("Error during animation data loading", tostring(result))
		end
		if ownsSession then
			self:_endLoadingSession()
		end
		return false
	end
end

-- Legacy-friendly loader: try binary first, then base64 text fallback.
function AnimationManager:loadAnimDataAuto(text: string, options: LoadOptions?)
	local progressContext, ownsSession = self:_getLoadingContext(options, "Importing Animation", "Detecting animation format")
	local detail = if options then options.detail else ""
	progressContext:set(0, "Detecting animation format", detail, false)

	if self:loadAnimDataFromText(text, true, {
		progress = progressContext:child(0, 1),
		detail = detail,
		suppressErrors = true,
	}) then
		progressContext:set(1, "Animation loaded", detail, true)
		if ownsSession then
			task.wait()
			self:_endLoadingSession()
		end
		return true
	end

	progressContext:set(0, "Retrying as text animation", detail, false)
	local success = self:loadAnimDataFromText(text, false, {
		progress = progressContext:child(0, 1),
		detail = detail,
		suppressErrors = if options then options.suppressErrors == true else false,
	})

	if success then
		progressContext:set(1, "Animation loaded", detail, true)
	end

	if ownsSession then
		if success then
			task.wait()
		end
		self:_endLoadingSession()
	end

	return success
end

-- Apply current bone toggle weights after any scaling so the final sequence honors UI toggles.
-- Only OVERRIDE when the user has explicitly disabled a bone; otherwise preserve
-- the animation's original Weight (which can be 0 for structural/passthrough poses).
local function applyBoneWeights(sequence: KeyframeSequence, rig: any)
	if not rig or not rig.bones then
		return
	end

	for _, keyframe in ipairs(sequence:GetKeyframes()) do
		for _, pose in ipairs(keyframe:GetDescendants()) do
			if pose:IsA("Pose") then
				local rigBone = rig.bones[pose.Name]
				if rigBone and not rigBone.enabled then
					pose.Weight = 0
				end
			end
		end
	end
end

local function syncRigAnimationFromKeyframeSequence(animationSerializerService, rig: any, sequence: KeyframeSequence)
	local animData = animationSerializerService:serialize(sequence, rig)
	if not animData then
		error("Failed to serialize KeyframeSequence for rig sync.")
	end

	rig:LoadAnimation(animData)
end

local function ensureActiveRigReady(progressContext: LoadingProgressContext?): boolean
	if State.activeRig then
		return true
	end

	local rigModel = State.activeRigModel or State.lastKnownRigModel
	if not rigModel then
		warn("No rig model available to rebuild active rig.")
		return false
	end

	local rigManager = State.rigManager
	if not rigManager or type(rigManager.setRig) ~= "function" then
		warn("RigManager unavailable; cannot rebuild active rig.")
		return false
	end

	if progressContext then
		progressContext:set(0.05, "Preparing rig preview", "rebuilding selected rig", false)
	end

	local ok, err = pcall(function()
		rigManager:setRig(rigModel)
	end)
	if not ok then
		warn("Failed to rebuild active rig:", err)
		return false
	end

	if not State.activeRig then
		warn("Active rig is still unavailable after rebuild.")
		return false
	end

	return true
end

function AnimationManager:loadRig(animationToLoad: KeyframeSequence?, progressContext: LoadingProgressContext?)
	self.playbackService:stopAnimationAndDisconnect()

	if not ensureActiveRigReady(progressContext) then
		warn("No active rig available")
		return false
	end

	local kfs: KeyframeSequence
	if animationToLoad then
		if progressContext then
			progressContext:set(0.15, "Preparing preview animation", "using selected animation", false)
		end
		kfs = animationToLoad:Clone()
		
		-- Restore Loop and Priority state from loaded KeyframeSequence
		State.loopAnimation:set(kfs.Loop)
		local priorityName = priorityReverseLookup[kfs.Priority]
		if priorityName then
			State.selectedPriority:set(priorityName)
		end
		
		-- Sync to Rig instance as well
		local rigAny = State.activeRig :: any
		rigAny.loop = kfs.Loop
		rigAny.priority = kfs.Priority
	else
		local activeRig = State.activeRig :: any
		kfs = activeRig:ToRobloxAnimation(
			if progressContext
				then function(progress: number, status: string?, detail: string?, canEstimate: boolean?)
					progressContext:set(progress * 0.5, status, detail, canEstimate)
				end
				else nil
		)
	end

	if progressContext then
		progressContext:set(0.58, "Preparing animation preview", "syncing keyframes", false)
	end

	if State.scaleFactor:get() ~= 1 then
		kfs = Utils.scaleAnimation(kfs, State.scaleFactor:get()) -- Scale the animation
	end

	-- Ensure the rig holds the loaded animation data so saving back to rig works (even for CurveAnimation-derived clips)
	pcall(function()
		syncRigAnimationFromKeyframeSequence(self.animationSerializerService, State.activeRig, kfs)
	end)

	-- Detect torso animation data on R6 rigs
	local function hasTorsoMotion(seq: KeyframeSequence): boolean
		local ok, result = pcall(function()
			for _, keyframe in ipairs(seq:GetKeyframes()) do
				local kf = keyframe :: Keyframe
				for _, pose in ipairs(kf:GetDescendants()) do
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

	applyBoneWeights(kfs, State.activeRig)

	-- Extract KeyframeMarkers and keyframe names from the loaded animation
	local extractedKeyframes = {}
	for _, keyframe in ipairs(kfs:GetKeyframes()) do
		local kf = keyframe :: Keyframe
		-- Check for KeyframeMarkers (Events) using proper Roblox API
		local markers = kf:GetMarkers()
		for _, marker in ipairs(markers) do
			local typedMarker = marker :: KeyframeMarker
			table.insert(extractedKeyframes, {
				name = typedMarker.Name,
				time = kf.Time,
				value = typedMarker.Value,
				type = "Event"
			})
		end
		-- Check for keyframe name (Name type)
		if kf.Name ~= "Keyframe" then
			table.insert(extractedKeyframes, {
				name = kf.Name,
				time = kf.Time,
				value = "",
				type = "Name"
			})
		end
	end
	table.sort(extractedKeyframes, function(a, b)
		return a.time < b.time
	end)
	State.keyframeNames:set(extractedKeyframes)

	State.animationData = (kfs:GetKeyframes() :: any) :: { Types.KeyframeType }?
	State.animationLength:set(Utils.getAnimDuration(State.animationData))
	
	-- Yield before playing large animations to prevent freezing
	local keyframes = (kfs:GetKeyframes() :: any) :: { Types.KeyframeType }?
	if keyframes and #keyframes > 500 then
		task.wait()
	end

	if progressContext then
		progressContext:set(
			0.92,
			"Starting animation preview",
			string.format("%d keyframes", keyframes and #keyframes or 0),
			true
		)
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
	if progressContext then
		progressContext:set(1, "Animation preview ready", string.format("%d keyframes", count), true)
	end
	return true
end

-- Helper function to add markers from State.keyframeNames to a KeyframeSequence
local function addMarkersToKeyframeSequence(kfs: KeyframeSequence)
	local keyframeNames = State.keyframeNames:get()
	if not keyframeNames or #keyframeNames == 0 then
		return
	end
	
	local keyframesByTime: { [number]: Keyframe } = {}
	for _, keyframe in ipairs(kfs:GetKeyframes()) do
		local kf = keyframe :: Keyframe
		keyframesByTime[kf.Time] = kf
	end
	
	local epsilon = 0.0001
	for _, kfData in ipairs(keyframeNames) do
		-- Find or create keyframe at this time
		local targetKeyframe: Keyframe? = nil
		for time, keyframe in pairs(keyframesByTime) do
			if math.abs(time - kfData.time) < epsilon then
				targetKeyframe = keyframe
				break
			end
		end
		
		-- If no keyframe exists at this time, create one
		if not targetKeyframe then
			local created = Instance.new("Keyframe")
			created.Time = kfData.time
			created.Parent = kfs
			keyframesByTime[kfData.time] = created
			targetKeyframe = created
		end
		if not targetKeyframe then
			continue
		end
		
		local markerType = kfData.type or "Name"
		if markerType == "Event" then
			local marker = Instance.new("KeyframeMarker")
			marker.Name = kfData.name
			marker.Value = (kfData.value and kfData.value ~= "") and kfData.value or ""
			targetKeyframe:AddMarker(marker)
		else
			targetKeyframe.Name = kfData.name
		end
	end
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

	-- Use currentKeyframeSequence for animation data, but add markers from State.keyframeNames
	local kfs
	if State.currentKeyframeSequence then
		kfs = State.currentKeyframeSequence:Clone()
		addMarkersToKeyframeSequence(kfs)
	else
		kfs = self:createKeyframeSequenceFromState()
	end
	if not kfs then
		return
	end

	-- Always apply current state properties (the clone path won't have these)
	kfs.Loop = State.loopAnimation:get()
	kfs.Priority = priorityLookup[State.selectedPriority:get()]

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
	
	-- Use currentKeyframeSequence for animation data, but add markers from State.keyframeNames
	local kfs
	if State.currentKeyframeSequence then
		kfs = State.currentKeyframeSequence:Clone()
		addMarkersToKeyframeSequence(kfs)
	else
		assert(State.activeRig)
		State.activeRig.keyframeNames = State.keyframeNames:get() :: { any }?
		kfs = State.activeRig:ToRobloxAnimation()
	end
	
	if State.scaleFactor:get() ~= 1 then
		kfs = Utils.scaleAnimation(kfs, State.scaleFactor:get()) -- Scale the animation
	end

	-- Always apply current state properties (the clone path won't have these)
	kfs.Loop = State.loopAnimation:get()
	kfs.Priority = priorityLookup[State.selectedPriority:get()]

	if name then
		kfs.Name = name
	end

	kfs.Parent = folder
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

	-- Use currentKeyframeSequence for animation data, but add markers from State.keyframeNames
	local kfs
	if State.currentKeyframeSequence then
		kfs = State.currentKeyframeSequence:Clone()
		addMarkersToKeyframeSequence(kfs)
	else
		kfs = self:createKeyframeSequenceFromState()
	end
	if not kfs then
		return
	end

	-- Always apply current state properties (the clone path won't have these)
	kfs.Loop = State.loopAnimation:get()
	kfs.Priority = priorityLookup[State.selectedPriority:get()]
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

	if self:loadRig(keyframeSequence) == false then
		warn("Failed to play saved animation because the active rig is not ready yet.")
	end
end

function AnimationManager:importAnimationsBulk()
	if State.activeRig then
		self.playbackService:stopAnimationAndDisconnect({ background = true })

		local animfiles = game:GetService("StudioService"):PromptImportFiles({ "rbxanim" })

		if animfiles then
			local totalFiles = #animfiles
			local importedCount = 0
			self:_beginLoadingSession(
				if totalFiles > 1 then "Bulk Importing Animations" else "Importing Animation",
				"Preparing files",
				string.format("0/%d selected", totalFiles)
			)
			local bulkProgress = createLoadingProgressContext(self, 0, 1)

			for index, animfile in ipairs(animfiles) do
				self.playbackService:stopAnimationAndDisconnect({ background = true })
				local fileName = animfile.Name
				local fileDetail = string.format("file %d/%d: %s", index, totalFiles, fileName)
				local fileProgress = bulkProgress:child((index - 1) / totalFiles, index / totalFiles)
				fileProgress:set(0, "Reading file", fileDetail, true)

				local loaded = (animfile :: any):GetBinaryContents()
				local ok, success = pcall(function()
					return self:loadAnimDataAuto(loaded, {
						progress = fileProgress,
						detail = fileDetail,
						suppressErrors = false,
					})
				end)

				if ok and success then
					importedCount += 1
					if totalFiles > 1 then
						local name = string.gsub(fileName, ".rbxanim", "")
						self:saveAnimationFolder(name)
					end
					fileProgress:set(1, "Imported animation", fileDetail, true)
				else
					warn("Error loading animation")
					fileProgress:set(1, "Skipped animation", fileDetail, true)
				end
			end

			bulkProgress:set(
				1,
				"Import finished",
				string.format("%d/%d animations imported", importedCount, totalFiles),
				true
			)
			self:_endLoadingSession()
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

-- Expose local helpers for unit testing
AnimationManager._testing = {
	sampleAxisValue = sampleAxisValue,
	interpolateMissingAxis = interpolateMissingAxis,
	ensureChannelSample = ensureChannelSample,
	applyBoneWeights = applyBoneWeights,
}

return AnimationManager



