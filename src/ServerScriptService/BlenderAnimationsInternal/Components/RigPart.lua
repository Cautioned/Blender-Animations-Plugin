--!native
--!strict
--!optimize 2

export type RigPart = {
	rig: any,
	part: Instance,
	parent: any?,
	joint: Instance?,
	bone: Bone?,
	poses: { [number]: any },
	children: { any },
	enabled: boolean,
	exportEnabled: boolean,
	isDeformRig: boolean,
	isDeformBone: boolean,
	jointParentIsPart0: boolean,
	jointType: string?,
}

local RigPart = {}
RigPart.__index = RigPart

local Pose = require(script.Parent.Pose)

local MAX_MOTOR6D_DEPTH = 1024 -- extreme depth guard to catch pathological rigs before Luau overflows

local function isAccessoryPart(inst: Instance): boolean
	local current: Instance? = inst
	while current do
		if current:IsA("Accessory") or current:IsA("Accoutrement") then
			return true
		end
		current = current.Parent
	end
	return false
end



type BuildState = {
	depth: number,
	maxDepth: number?,
	path: { Instance },
	pathSet: { [Instance]: boolean },
	visitedAll: { [Instance]: boolean },
}

local function formatCycle(state: BuildState, repeated: Instance)
	local cycleNames = {}
	local startIndex = 1
	for i = 1, #state.path do
		if state.path[i] == repeated then
			startIndex = i
			break
		end
	end
	for i = startIndex, #state.path do
		cycleNames[#cycleNames + 1] = state.path[i].Name
	end
	cycleNames[#cycleNames + 1] = repeated.Name
	return table.concat(cycleNames, " -> ")
end

function RigPart.new(
	rig: any,
	part: Instance,
	parent: any?,
	isDeformRig: boolean,
	connectingJoint: Instance?,
	buildState: BuildState?
): RigPart?
	if not parent then
		buildState = buildState
			or {
				depth = 0,
				maxDepth = MAX_MOTOR6D_DEPTH,
				path = {},
				pathSet = {},
				visitedAll = {},
			}
	end

	local state = buildState
	local trackCycle = (connectingJoint == nil) or (connectingJoint:IsA("Motor6D")) or (connectingJoint:IsA("AnimationConstraint"))
	if state and state.visitedAll[part] then
		return nil
	end
	if state and trackCycle then
		local nextDepth = (state.depth or 0) + 1
		local maxDepth = state.maxDepth or MAX_MOTOR6D_DEPTH
		if nextDepth > maxDepth then
			error(
				string.format(
					"Motor6D hierarchy exceeded safe depth (%d). Likely cycle near '%s'.",
					maxDepth,
					part:GetFullName()
				)
			)
		end
		if state.pathSet[part] then
			error("CIRCULAR MOTOR6D TRAVERSAL DETECTED: " .. formatCycle(state, part))
		end
		state.depth = nextDepth
		state.pathSet[part] = true
		state.path[#state.path + 1] = part
	end
	local self: RigPart = {
		rig = rig,
		part = part,
		parent = parent,
		joint = nil,
		bone = nil, -- Store the bone object explicitly
		poses = {},
		children = {},
		enabled = true,
		exportEnabled = true,
		isDeformRig = isDeformRig or false, -- Flag if this is part of a deform rig
		isDeformBone = false,
		jointParentIsPart0 = true,
		jointType = nil,
	}
	setmetatable(self, RigPart)

	rig.bonesByInstance = rig.bonesByInstance or {}
	rig.bonesByInstance[part] = self
	if state then
		state.visitedAll[part] = true
	end
	
	-- Debug print to check part type
	
	if parent then
		if isDeformRig and part:IsA("Bone") then
			-- For Bone objects, we don't need to find a Motor6D joint
			-- The bone itself contains the transform information
			self.bone = part -- Store the bone object
			self.jointType = "Bone"
			-- print("Setting bone for", part.Name)
		else
			-- Traditional joint (Motor6D/Weld/WeldConstraint/AnimationConstraint)
			local joint: Instance? = connectingJoint
			if not joint and rig._jointCache and rig._jointCache[part] then
				for _, candidate in ipairs(rig._jointCache[part]) do
					if candidate.Part0 == parent.part and candidate.Part1 == part then
						joint = candidate
						break
					elseif candidate.Part1 == parent.part and candidate.Part0 == part then
						joint = candidate
						break
					end
				end
			end
			if joint then
				self.joint = joint
				self.jointParentIsPart0 = ((joint :: any).Part0 == parent.part)
				self.jointType = joint.ClassName
			end
			-- if self.joint then
			-- 	-- print("Found Motor6D joint for", part.Name, "Joint:", self.joint.Name)
			-- else
			-- 	-- print("No Motor6D joint found for", part.Name)
			-- end
		end
	end

	-- Always look for joint-connected children (Motor6D/Weld/WeldConstraint)
	for _, joint in pairs(rig._jointCache[part] or {}) do
		if joint.Part0 and joint.Part1 then
			local subpart
			if joint.Part0 == part then
				subpart = joint.Part1
			elseif joint.Part1 == part then
				subpart = joint.Part0
			end
			if subpart and (not parent or subpart ~= parent.part) then
				local child = RigPart.new(rig, subpart, self, isDeformRig, joint, state)
				if child then
					table.insert(self.children, child)
				end
			end
		end
	end

	-- If this is a deform rig, also look for Bone children
	if isDeformRig and part:IsA("BasePart") then
		for _, child in pairs(part:GetChildren()) do
			if child:IsA("Bone") then
				-- We no longer create a RigPart for the bone here,
				-- as that is handled by Rig:buildBoneHierarchy
			end
		end
	end

	local existing = rig.bones[part.Name]
	if existing == nil or existing == self then
		rig.bones[part.Name] = self
	else
		local preferNew = false
		if self.bone and not existing.bone then
			preferNew = true
		elseif self.bone and existing.bone and existing.part ~= part then
			preferNew = true
		end

		if preferNew then
			rig.bones[part.Name] = self
		else
			rig._duplicateBoneWarnings = rig._duplicateBoneWarnings or {}
			if not rig._duplicateBoneWarnings[part.Name] then
				rig._duplicateBoneWarnings[part.Name] = true
				if not isAccessoryPart(part) then
					warn("Duplicate rig part name detected:", part.Name, "for model", rig.model and rig.model.Name or "<unknown>")
				end
			end
		end
	end

	if state and trackCycle then
		state.depth = state.depth - 1
		state.pathSet[part] = nil
		state.path[#state.path] = nil
	end

	return (self :: any)
end

function RigPart:AddPose(kft, transform, isDeformBone, easingStyle, easingDirection)
	-- print("Adding pose at time", kft, "for", self.part.Name, "Bone:", self.bone ~= nil)
	self.poses[kft] = Pose.new(self, transform, easingStyle, easingDirection)
end

function RigPart:PoseToRobloxAnimation(t)
	local poses = self.poses
	local poseToApply = poses[t]
	local children = self.children
	local part = self.part
	local enabled = self.enabled

	local childrenPoses = {}
	for _, child in pairs(children) do
		local subpose = (child :: any):PoseToRobloxAnimation(t)
		if subpose then
			table.insert(childrenPoses, subpose)
		end
	end

	-- If this part has no keyframe at this exact time, synthesize one so
	-- Roblox doesn't treat the missing Pose as CFrame.identity (which would
	-- snap the bone to rest mid-animation).
	if not poseToApply then
		local prevTime, nextTime = nil, nil
		for poseTime, _ in pairs(poses) do
			if poseTime < t then
				if prevTime == nil or poseTime > prevTime then
					prevTime = poseTime
				end
			end
			if poseTime > t then
				if nextTime == nil or poseTime < nextTime then
					nextTime = poseTime
				end
			end
		end

		local prevPose = prevTime and poses[prevTime] or nil
		local nextPose = nextTime and poses[nextTime] or nil

		if prevPose then
			local easingStyle = prevPose.easingStyle or "Linear"
			if easingStyle == "Constant" or not nextPose or nextTime == nil then
				-- Constant easing or no future keyframe: hold previous value
				poseToApply = prevPose
			else
				-- Interpolate between prev and next (Linear/other)
				local alpha = (t - (prevTime :: number)) / ((nextTime :: number) - (prevTime :: number))
				local interpCFrame = prevPose.transform:Lerp(nextPose.transform, alpha)
				poseToApply = {
					transform = interpCFrame,
					-- Carry forward prev's easing so the segment from this
					-- synthetic keyframe to the next real one stays consistent
					easingStyle = easingStyle,
					easingDirection = prevPose.easingDirection or "In",
				}
			end
		elseif nextPose then
			-- Before the bone's first keyframe: use the next available pose.
			-- Roblox will interpolate from this value forward, so projecting
			-- the first real keyframe backwards keeps the bone stable until
			-- its first actual keyframe is reached.
			poseToApply = nextPose
		end
		-- else: bone has no poses at all; poseToApply stays nil → identity below

		-- If no pose and no children, prune this branch entirely
		if not poseToApply and #childrenPoses == 0 then
			return nil
		end
	end

	local pose = Instance.new("Pose")
	pose.Name = part.Name
	pose.Weight = enabled and 1 or 0

	if poseToApply then
		local transform = poseToApply.transform
		pose.CFrame = transform

		-- Apply easing styles and directions directly using enum values
		if poseToApply.easingStyle then
			-- Direct assignment using pcall to handle any invalid values gracefully
			local success, style = pcall(function()
				return Enum.PoseEasingStyle:FromName(poseToApply.easingStyle)
			end)
			if success and style then
				pose.EasingStyle = style
			else
				warn("Invalid easing style:", poseToApply.easingStyle, "for part:", part.Name)
				pose.EasingStyle = Enum.PoseEasingStyle.Linear -- Fallback to Linear
			end
		end

		if poseToApply.easingDirection then
			-- Direct assignment using pcall to handle any invalid values gracefully
			local success, dir = pcall(function()
				return Enum.PoseEasingDirection:FromName(poseToApply.easingDirection)
			end)
			if success and dir then
				pose.EasingDirection = dir
			else
				warn("Invalid easing direction:", poseToApply.easingDirection, "for part:", part.Name)
				pose.EasingDirection = Enum.PoseEasingDirection.In -- Fallback to In
			end
		end
	end

	for _, subpose in ipairs(childrenPoses) do
		subpose.Parent = pose
	end

	return pose
end

function RigPart:ApplyPose(t)
	local poses = self.poses
	local pose = poses[t]
	
	if pose then
		local transform = pose.transform :: CFrame
		local bone = self.bone
		local joint = self.joint
		local enabled = self.enabled

		if (bone or (joint and joint:IsA("AnimationConstraint"))) and enabled then
			-- For deform bones and AnimationConstraints, the transform from the addon is applied directly
			if bone then
				bone.Transform = transform
			else
				-- AnimationConstraint uses Transform property directly like bones
				(joint :: any).Transform = transform
			end
		elseif joint and joint:IsA("Motor6D") and enabled then
			if self.jointParentIsPart0 then
				joint.C0 = transform * joint.C1:Inverse()
			else
				joint.C1 = transform * joint.C0
			end
		elseif not enabled then
			-- Debug: log when a bone or joint is disabled
			if bone then
				print("Skipping disabled bone:", self.part.Name)
			elseif joint and joint:IsA("AnimationConstraint") then
				print("Skipping disabled AnimationConstraint:", self.part.Name)
			elseif joint and joint:IsA("Motor6D") then
				print("Skipping disabled Motor6D:", self.part.Name)
			end
		end
	end

	-- Always process children, even if this part has no pose
	local children = self.children
	for _, child in pairs(children) do
		(child :: any):ApplyPose(t)
	end
end

function RigPart:FindAuxPartsLegacy()
	-- For Bone objects, we don't need to find auxiliary parts
	local bone = self.bone
	if bone then
		return { self.part }
	end

	local part = self.part
	local model = self.rig.model

	local jointSet = {}
	for _, joint in ipairs(model:GetDescendants()) do
		local asJoint = joint :: any
		if joint:IsA("JointInstance") and not joint:IsA("Motor6D") then
			if asJoint.Part0 == part or asJoint.Part1 == part then
				table.insert(jointSet, joint)
			end
		end
	end

	local instSet = {}
	for i, joint in pairs(jointSet) do
		local asJoint = joint :: any
		instSet[i] = asJoint.Part0 == part and asJoint.Part1 or asJoint.Part0
	end
	instSet[#instSet + 1] = part

	return instSet
end

function RigPart:Encode(handledParts, opts)
	if self.exportEnabled == false then
		return nil
	end

	handledParts = handledParts or {}
	opts = opts or {}
	local exportWelds = opts.exportWelds == true
	
	-- Skip parts connected by Weld/WeldConstraint when exportWelds is disabled
	-- AnimationConstraint is treated like Motor6D and always exported
	local joint = self.joint
	if not exportWelds and joint and (joint:IsA("Weld") or joint:IsA("WeldConstraint")) then
		return nil
	end

	local part = self.part
	handledParts[part] = true

	local elem = {
		inst = part,
		jname = part.Name,
		children = {},
		aux = {},
		isDeformBone = self.bone ~= nil,
		jointType = (nil :: string?),
		auxTransform = {},
	}

	-- Legacy aux parts export (now controlled by exportWelds since it was used for the same purpose)
	if exportWelds then
		local auxInsts = self:FindAuxPartsLegacy()
		elem.aux = auxInsts
		for i, auxInst in ipairs(auxInsts) do
			local cframe = nil
			if auxInst:IsA("BasePart") then
				cframe = auxInst.CFrame
			end
			elem.auxTransform[i] = cframe and { cframe:GetComponents() } or nil
		end
	end

	local bone = self.bone
	if bone then
		-- This is a deform bone. We will make it look like a Motor6D joint
		-- by sending its WorldCFrame and creating virtual joint data.
		elem.transform = { bone.WorldCFrame:GetComponents() }
		local boneParent = self.parent
		if boneParent then
			-- The bone's local CFrame becomes C0. C1 is identity.
			elem.jointtransform0 = { bone.CFrame:GetComponents() }
			elem.jointtransform1 = { CFrame.new():GetComponents() }
		end
		elem.jointType = "Bone"
	else
		-- This is a BasePart connected by Motor6D (or the root).
		-- Send its world CFrame.
		elem.transform = { part.CFrame:GetComponents() }
		-- If it's a child, also send the real joint data.
		local partParent = self.parent
		local jointInstance = self.joint
		if partParent and jointInstance then
			if jointInstance:IsA("Motor6D") or jointInstance:IsA("Weld") then
				-- IMPORTANT: Normalize C0/C1 based on joint direction.
				-- jointtransform0 should always be relative to the PARENT part.
				-- jointtransform1 should always be relative to the CHILD part.
				-- When jointParentIsPart0=true: Part0=parent, Part1=child -> C0 is parent-relative, C1 is child-relative (standard)
				-- When jointParentIsPart0=false: Part0=child, Part1=parent -> C0 is child-relative, C1 is parent-relative (swapped)
				if self.jointParentIsPart0 then
					elem.jointtransform0 = { (jointInstance :: any).C0:GetComponents() }
					elem.jointtransform1 = { (jointInstance :: any).C1:GetComponents() }
				else
					-- Swap: C1 becomes jointtransform0 (parent-relative), C0 becomes jointtransform1 (child-relative)
					elem.jointtransform0 = { (jointInstance :: any).C1:GetComponents() }
					elem.jointtransform1 = { (jointInstance :: any).C0:GetComponents() }
				end
			elseif jointInstance:IsA("AnimationConstraint") then
				-- For AnimationConstraint, the Transform property acts like C0, C1 is identity
				elem.jointtransform0 = { (jointInstance :: any).Transform:GetComponents() }
				elem.jointtransform1 = { CFrame.new():GetComponents() }
			elseif jointInstance:IsA("WeldConstraint") then
				-- WeldConstraint doesn't have C0/C1, use relative transform
				local parentToChild = (jointInstance :: any).Part0.CFrame:ToObjectSpace((jointInstance :: any).Part1.CFrame)
				elem.jointtransform0 = { parentToChild:GetComponents() }
				elem.jointtransform1 = { CFrame.new():GetComponents() }
			end
			elem.jointType = jointInstance.ClassName
		end
	end

	local children = self.children
	local childCount = 0
	for _, subrigpart in pairs(children) do
		childCount = childCount + 1
		if childCount % 50 == 0 then
			task.wait()
		end

		if not handledParts[subrigpart.part] then
			local encodedChild = (subrigpart :: any):Encode(handledParts, opts)
			if encodedChild then
				table.insert(elem.children, encodedChild)
			end
		end
	end

	return elem
end

return RigPart
