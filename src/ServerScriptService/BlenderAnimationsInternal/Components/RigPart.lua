--!native
--!strict
--!optimize 2

export type RigPart = {
	rig: any,
	part: Instance,
	parent: any?,
	joint: Motor6D?,
	bone: Bone?,
	poses: { [number]: any },
	children: { any },
	enabled: boolean,
	isDeformRig: boolean,
	isDeformBone: boolean,
}

local RigPart = {}
RigPart.__index = RigPart

local Pose = require(script.Parent.Pose)



function RigPart.new(rig: any, part: Instance, parent: any?, isDeformRig: boolean)
	local self: RigPart = {
		rig = rig,
		part = part,
		parent = parent,
		joint = nil,
		bone = nil, -- Store the bone object explicitly
		poses = {},
		children = {},
		enabled = true,
		isDeformRig = isDeformRig or false, -- Flag if this is part of a deform rig
		isDeformBone = false,
	}
	setmetatable(self, RigPart)

	rig.bones[part.Name] = self
	
	-- Debug print to check part type
	
	if parent then
		if isDeformRig and part:IsA("Bone") then
			-- For Bone objects, we don't need to find a Motor6D joint
			-- The bone itself contains the transform information
			self.bone = part -- Store the bone object
			-- print("Setting bone for", part.Name)
		else
			-- Traditional Motor6D joint
			if rig._jointCache and rig._jointCache[part] then
				for _, joint in ipairs(rig._jointCache[part]) do
					if joint.Part0 == parent.part then
						self.joint = joint
						break
					end
				end
			end
			-- if self.joint then
			-- 	-- print("Found Motor6D joint for", part.Name, "Joint:", self.joint.Name)
			-- else
			-- 	-- print("No Motor6D joint found for", part.Name)
			-- end
		end
	end

	-- Always look for Motor6D-connected children
	for _, joint in pairs(rig._jointCache[part] or {}) do
		if joint.Part0 and joint.Part1 then
			local subpart = joint.Part0 == part and joint.Part1 or joint.Part0
			if subpart and (not parent or subpart ~= parent.part) then
				table.insert(self.children, RigPart.new(rig, subpart, self, isDeformRig))
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

	return self
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

	-- If this part has no keyframe at this time, AND no children have poses, prune it.
	if not poseToApply and #childrenPoses == 0 then
		return nil
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

		if bone then
			-- For all deform bones, the transform from the addon is the delta to apply directly.
			bone.Transform = transform
		elseif joint and joint:IsA("Motor6D") and enabled then
			-- Only apply to Motor6D joints if enabled
			joint.C0 = transform * joint.C1:Inverse()
		end
	end

	-- Always process children, even if this part has no pose
	local children = self.children
	for _, child in pairs(children) do
		(child :: any):ApplyPose(t)
	end
end

function RigPart:FindAuxParts()
	-- For Bone objects, we don't need to find auxiliary parts
	local bone = self.bone
	if bone then
		return { self.part }
	end

	local part = self.part
	local rig = self.rig
	local model = rig.model

	local jointSet = {}
	for _, joint in ipairs(model:GetDescendants()) do
		if
			joint:IsA("JointInstance")
			and not joint:IsA("Motor6D")
			and (joint.Part0 == part or joint.Part1 == part)
		then
			table.insert(jointSet, joint)
		end
	end

	local instSet = {}
	for i, joint in pairs(jointSet) do
		instSet[i] = joint.Part0 == part and joint.Part1 or joint.Part0
	end
	instSet[#instSet + 1] = part

	return instSet
end

function RigPart:Encode(handledParts)
	handledParts = handledParts or {}
	local part = self.part
	handledParts[part] = true

	local elem = {
		inst = part,
		jname = part.Name,
		children = {},
		aux = self:FindAuxParts(),
		isDeformBone = self.bone ~= nil,
	}

	local bone = self.bone
	if bone then
		-- This is a deform bone. We will make it look like a Motor6D joint
		-- by sending its WorldCFrame and creating virtual joint data.
		elem.transform = { bone.WorldCFrame:GetComponents() }
		local parent = self.parent
		if parent then
			-- The bone's local CFrame becomes C0. C1 is identity.
			elem.jointtransform0 = { bone.CFrame:GetComponents() }
			elem.jointtransform1 = { CFrame.new():GetComponents() }
		end
	else
		-- This is a BasePart connected by Motor6D (or the root).
		-- Send its world CFrame.
		elem.transform = { part.CFrame:GetComponents() }
		-- If it's a child, also send the real joint data.
		local parent = self.parent
		local joint = self.joint
		if parent and joint then
			elem.jointtransform0 = { joint.C0:GetComponents() }
			elem.jointtransform1 = { joint.C1:GetComponents() }
		end
	end

	local children = self.children
	for _, subrigpart in pairs(children) do
		if not handledParts[subrigpart.part] then
			table.insert(elem.children, (subrigpart :: any):Encode(handledParts))
		end
	end

	return elem
end

return RigPart
