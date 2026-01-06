--!strict
--!optimize 2

local Rig = {}
Rig.__index = Rig

local RigPart = require(script.Parent.RigPart)

type self = {
	model: Model,
	root: RigPart.RigPart?,
	animTime: number,
	loop: boolean,
	priority: Enum.AnimationPriority,
	keyframeNames: { { t: number, name: string } },
	bones: { [string]: RigPart.RigPart },
	bonesByInstance: { [Instance]: RigPart.RigPart },
	isDeformRig: boolean,
	boneHierarchy: { [string]: string? },
	_jointCache: { [Instance]: { Motor6D } },
	_duplicateBoneWarnings: { [string]: boolean }?,
}

function Rig.new(model: Model)
	local self: self = {
		model = model,
		root = nil,
		animTime = 10,
		loop = true,
		priority = Enum.AnimationPriority.Action,
		keyframeNames = {}, -- table with values each in the format: {t = number, name = string}
		bones = {}, -- Initialize bones property
		bonesByInstance = {},
		isDeformRig = false, -- Flag to indicate if this is a deform bone rig
		boneHierarchy = {}, -- Store bone parent relationships
		_jointCache = {},
		_duplicateBoneWarnings = {},
	}
	setmetatable(self, Rig)

	-- Single traversal to gather all necessary descendants
	local allBones = {}
	local allJoints = {}
	for _, descendant in ipairs(model:GetDescendants()) do
		if descendant:IsA("Bone") then
			table.insert(allBones, descendant)
		elseif descendant:IsA("Motor6D") then
			table.insert(allJoints, descendant)
		end
	end

	self.isDeformRig = #allBones > 0

    -- Pre-build the joint cache for fast lookups, guarding duplicate Motor6D names under the same parent
	self._jointCache = {}
    local duplicateGuard: { [Instance]: { [string]: boolean } } = {}
	for _, joint in ipairs(allJoints) do
        local parentInst = joint.Parent
        if parentInst then
            local nameSet = duplicateGuard[parentInst]
            if not nameSet then
                nameSet = {}
                duplicateGuard[parentInst] = nameSet
            end
            if nameSet[joint.Name] then
                warn("duplicate Motor6D name under same parent detected; ignoring subsequent joint:", parentInst:GetFullName(), joint.Name)
                -- do not index this duplicate into caches to avoid ambiguity
            else
                nameSet[joint.Name] = true
		local p0, p1 = joint.Part0, joint.Part1
		if p0 then
			if not self._jointCache[p0] then
				self._jointCache[p0] = {}
			end
			table.insert(self._jointCache[p0], joint)
		end
		if p1 then
			if not self._jointCache[p1] then
				self._jointCache[p1] = {}
			end
			table.insert(self._jointCache[p1], joint)
                end
            end
		end
	end

	-- Check for cyclic motor6d dependencies before building hierarchy
	self:checkCyclicMotor6D(model)
	
	-- Always build the Motor6D hierarchy first, if a root exists.
	if model.PrimaryPart then
		self.root = RigPart.new(self, model.PrimaryPart, nil, self.isDeformRig)
	else
		warn("Model has no PrimaryPart for traditional rig setup. Rig root will be nil.")
		self.root = nil
	end

	-- If it's a deform rig, find all bones and add them to the rig.
	-- This assumes bones are parented to parts that are already in the rig.
	if self.isDeformRig then
		self:buildBoneHierarchy(allBones)
	end

	return self
end

function Rig:checkCyclicMotor6D(model: Model)
	-- Build a graph of motor6d connections
	local motor6dGraph: { [BasePart]: { BasePart } } = {}
	local allParts: { BasePart } = {}
	
	-- Collect all motor6d joints and build the graph
	for _, descendant in ipairs(model:GetDescendants()) do
		if descendant:IsA("Motor6D") then
			local motor6d = descendant :: Motor6D
			local part0 = motor6d.Part0
			local part1 = motor6d.Part1
			
			if part0 and part1 then
				if not motor6dGraph[part0] then
					motor6dGraph[part0] = {}
					table.insert(allParts, part0)
				end
				if not motor6dGraph[part1] then
					motor6dGraph[part1] = {}
					table.insert(allParts, part1)
				end
				
				-- Add directed edge from part0 to part1
				table.insert(motor6dGraph[part0], part1)
			end
		end
	end
	
	-- Use DFS to detect cycles
	local visited: { [BasePart]: boolean } = {}
	local recStack: { [BasePart]: boolean } = {}
	local cyclePath: { BasePart } = {}
	
	local function hasCycleDFS(part: BasePart): boolean
		visited[part] = true
		recStack[part] = true
		table.insert(cyclePath, part)
		
		local neighbors = motor6dGraph[part] or {}
		for _, neighbor in ipairs(neighbors) do
			if not visited[neighbor] then
				if hasCycleDFS(neighbor) then
					return true
				end
			elseif recStack[neighbor] then
				-- Found a cycle! Build the cycle description
				local cycleDescription = {}
				local startIndex = 1
				for i, p in ipairs(cyclePath) do
					if p == neighbor then
						startIndex = i
						break
					end
				end
				
				for i = startIndex, #cyclePath do
					table.insert(cycleDescription, cyclePath[i].Name)
				end
				table.insert(cycleDescription, neighbor.Name) -- Close the cycle
				
				error("CIRCULAR MOTOR6D CHAIN DETECTED: " .. table.concat(cycleDescription, " -> ") .. 
					". This creates an infinite loop that will break rigs. Fix by removing one of the motor6d connections in this chain.")
			end
		end
		
		table.remove(cyclePath) -- Remove current part from path
		recStack[part] = false
		return false
	end
	
	-- Check each part for cycles
	for _, part in ipairs(allParts) do
		if not visited[part] then
			cyclePath = {} -- Reset path for each new DFS
			hasCycleDFS(part)
		end
	end
end





function Rig:buildBoneHierarchy(allBones)
	-- Find all bones in the model and create RigParts for them.
	-- This now ADDS to the rig rather than creating it from scratch.
    -- Guard against cyclic bone parenting (depth issues) using Kahn's algorithm (iterative, no recursion)
    do
        local boneSet: { [Instance]: boolean } = {}
        for i = 1, #allBones do
            boneSet[allBones[i]] = true
        end
        -- initialize all bones in graph with indegree 0
        local graph: { [Instance]: { Instance } } = {}
        local indegree: { [Instance]: number } = {}
        for i = 1, #allBones do
            local b = allBones[i]
            indegree[b] = 0
            graph[b] = {}
        end
        -- build edges: bone -> child bone
        for i = 1, #allBones do
            local b = allBones[i]
            local p = b.Parent
            -- only add edge if parent is also a bone (bone-to-bone cycle check)
            if p and boneSet[p] then
                if not graph[p] then
                    graph[p] = {}
                    indegree[p] = indegree[p] or 0
                end
                table.insert(graph[p], b)
                indegree[b] = (indegree[b] or 0) + 1
            end
        end
        -- kahn's: queue all zero-indegree nodes
        local queue: { Instance } = {}
        local qh, qt = 1, 0
        for i = 1, #allBones do
            local b = allBones[i]
            if indegree[b] == 0 then
                qt += 1
                queue[qt] = b
            end
        end
        local processed = 0
        while qh <= qt do
            local n = queue[qh]
            qh += 1
            processed += 1
            local nbrs = graph[n]
            if nbrs then
                for i = 1, #nbrs do
                    local m = nbrs[i]
                    indegree[m] -= 1
                    if indegree[m] == 0 then
                        qt += 1
                        queue[qt] = m
                    end
                end
            end
        end
        -- if we didn't process all bones, there's a cycle
        if processed < #allBones then
            error("CIRCULAR BONE HIERARCHY DETECTED: remove the cycle in bone parenting.")
        end
    end
    -- intentionally unused local kept for readability when editing
    local _bones = self.bones

	local unresolved = {}
	for _, bone in ipairs(allBones) do
		unresolved[#unresolved + 1] = bone
	end

	while #unresolved > 0 do
		local resolvedThisPass = false
        -- attempt to resolve all items; if none resolved, break to avoid infinite loop
		for i = #unresolved, 1, -1 do
			local bone = unresolved[i]
			local parentInstance = bone.Parent
			local parentPart = nil
			if parentInstance then
				parentPart = self:FindRigPartByInstance(parentInstance) or self:FindRigPart(parentInstance.Name)
			end

			if parentPart then
				local rigPart = RigPart.new(self, bone, parentPart, true)
				table.insert(parentPart.children, rigPart)
				if self.bones[bone.Name] == nil then
					self.bones[bone.Name] = rigPart
				end
				table.remove(unresolved, i)
				resolvedThisPass = true
			end
		end

		if not resolvedThisPass then
			for _, bone in ipairs(unresolved) do
				local parentInstance = bone.Parent
				warn(
					"Could not resolve parent rig part for bone:",
					bone.Name,
					"Parent:",
					parentInstance and parentInstance.Name or "<nil>"
				)
			end
			break
		end
	end
end



function Rig:GetRigParts()
	local parts = {}
	local root = self.root

    if not root then
        return parts
    end

    -- iterative dfs to avoid recursion limits on deep hierarchies; guards cycles too
    local stack = { root }
    local visited = {}

    while #stack > 0 do
        local current = stack[#stack]
        stack[#stack] = nil

        if not visited[current] then
            visited[current] = true
		
            for _, child in pairs(current.children) do
			parts[#parts + 1] = child
                stack[#stack + 1] = child
            end
		end
	end

	return parts
end

function Rig:FindRigPart(name)
	return self.bones[name]
end

function Rig:FindRigPartByInstance(instance: Instance)
	return self.bonesByInstance and self.bonesByInstance[instance] or nil
end

function Rig:ClearPoses()
	for _, rigPart in pairs(self:GetRigParts()) do
		rigPart.poses = {}
	end
end

function Rig:LoadAnimation(data)
	-- Validate animation data structure
	if not data then
		error("Animation data is nil")
	end

	if type(data) ~= "table" then
		error("Animation data is not a table, got: " .. type(data))
	end

	if not data.kfs or type(data.kfs) ~= "table" then
		error("Animation data missing keyframes array (data.kfs)")
	end

	self:ClearPoses()

	local rigParts = self:GetRigParts()
	
	local isDeformRig = self.isDeformRig

	self.animTime = data.t

	-- Check if this is a deform rig animation
	if data.is_deform_rig then
		self.isDeformRig = true
		isDeformRig = true

		-- If we have bone hierarchy data, update our hierarchy
		if data.bone_hierarchy then
			self.boneHierarchy = data.bone_hierarchy

			-- Ensure our RigPart hierarchy matches the bone hierarchy
			for boneName, parentName in pairs(data.bone_hierarchy) do
				local bonePart = self:FindRigPart(boneName)
				if bonePart then
					bonePart.isDeformBone = true

					-- Set parent relationship if parent exists
					if parentName then
						local parentPart = self:FindRigPart(parentName)
						if parentPart then
							-- Remove from current parent's children
							if bonePart.parent then
								for i, child in pairs(bonePart.parent.children) do
									if child == bonePart then
										table.remove(bonePart.parent.children, i)
										break
									end
								end
							end

							-- Add to new parent's children
							bonePart.parent = parentPart
							table.insert(parentPart.children, bonePart)
						end
					end
				end
			end
		end
	end

	for _, kfdef in pairs(data.kfs) do
		-- Validate keyframe data
		if not kfdef.t or type(kfdef.t) ~= "number" then
			warn("Skipping keyframe with invalid time value")
			continue
		end

		if not kfdef.kf or type(kfdef.kf) ~= "table" then
			warn("Skipping keyframe with invalid pose data at time " .. kfdef.t)
			continue
		end

		for _, rigPart in pairs(rigParts) do
			local partName = rigPart.part.Name
			local poseData = kfdef.kf[partName]
			
            if poseData then
				local cfc
				local easingStyle = "Linear" -- Default
				local easingDirection = "In" -- Default

                -- Accept multiple formats:
                -- 1) New array: [ [components], "EasingStyle"?, "EasingDirection"? ]
                -- 2) New object: { components = {...}, easingStyle? = "", easingDirection? = "" }
                -- 3) Legacy: {components...} flat list
                if type(poseData) == "table" then
                    if type(poseData[1]) == "table" then
                        -- array form with nested components
                        cfc = poseData[1]
                        if poseData[2] ~= nil then easingStyle = poseData[2] end
                        if poseData[3] ~= nil then easingDirection = poseData[3] end
                    elseif poseData.components ~= nil then
                        -- object/dict form
                        cfc = poseData.components
                        if poseData.easingStyle ~= nil then easingStyle = poseData.easingStyle end
                        if poseData.easingDirection ~= nil then easingDirection = poseData.easingDirection end
                    else
                        -- legacy: assume flat list
                        cfc = poseData
                    end
				else
					-- Fallback for old format (just cframe components)
					cfc = poseData
				end

				-- Validate CFrame data
				if type(cfc) ~= "table" or #cfc < 12 then
					warn("Invalid CFrame data for part " .. partName .. " at time " .. kfdef.t)
					continue
				end

				-- Ensure all CFrame values are numbers
				for i = 1, 12 do
					if type(cfc[i]) ~= "number" then
						warn("Non-numeric value in CFrame for part " .. partName .. " at time " .. kfdef.t)
						cfc[i] = tonumber(cfc[i]) or 0
					end
				end

				-- Check if this is a deform bone marker
				local isDeformBone = false
				if kfdef.kf[partName .. "_deform"] or (isDeformRig and rigPart.part:IsA("Bone")) then
					isDeformBone = true
					rigPart.isDeformBone = true
				end

				-- normalize each rotation vector, falling back to canonical axes if the vector is degenerate
				for axis = 0, 2 do
					local x = cfc[4 + axis]
					local y = cfc[7 + axis]
					local z = cfc[10 + axis]
					local lengthSq = x * x + y * y + z * z
					if lengthSq > 1e-8 then
						local invLen = 1 / math.sqrt(lengthSq)
						x *= invLen
						y *= invLen
						z *= invLen
					else
						if axis == 0 then
							x, y, z = 1, 0, 0
						elseif axis == 1 then
							x, y, z = 0, 1, 0
						else
							x, y, z = 0, 0, 1
						end
					end
					cfc[4 + axis], cfc[7 + axis], cfc[10 + axis] = x, y, z
				end

				rigPart:AddPose(kfdef.t, CFrame.new(unpack(cfc)), isDeformBone, easingStyle, easingDirection)
			end
		end
	end
end

function Rig:ToRobloxAnimation()
	if not self.root then
		return nil
	end
	local kfs = Instance.new("KeyframeSequence")
	kfs.Loop = self.loop
	kfs.Priority = self.priority
	local humanoid = self.model:FindFirstChildOfClass("Humanoid")
	if humanoid then -- otherwise just use default/is anim controller/...
		kfs.AuthoredHipHeight = humanoid.HipHeight
	end

	local allRigParts = self:GetRigParts()
	if self.root then
		table.insert(allRigParts, 1, self.root) -- Add root to the beginning of the list to check.
	end

	local keyframeNames = self.keyframeNames or {}
	table.sort(keyframeNames, function(a, b)
		return a.time < b.time
	end) -- Ensure names are sorted by time

	-- Collect all unique time points from poses and named events
	local timePoints = { [0] = true } -- Always have a keyframe at t=0
	for _, rigPart in pairs(allRigParts) do
		for poseT, _ in pairs(rigPart.poses) do
			timePoints[poseT] = true
		end
	end
	for _, kfName in pairs(keyframeNames) do
		timePoints[kfName.time] = true
	end

	local sortedTimes = {}
	for t in pairs(timePoints) do
		table.insert(sortedTimes, t)
	end
	table.sort(sortedTimes)

	local nextKfNameIdx = 1

	for _, t in ipairs(sortedTimes) do
		-- Serialize t
		local kf = Instance.new("Keyframe")
		kf.Time = t
		kf.Parent = kfs

		-- This loop handles multiple named keyframes at the exact same time point
		while keyframeNames[nextKfNameIdx] and keyframeNames[nextKfNameIdx].time <= t do
			if keyframeNames[nextKfNameIdx].time == t then
				kf.Name = keyframeNames[nextKfNameIdx].name
			end
			nextKfNameIdx = nextKfNameIdx + 1
		end

		local pose = self.root:PoseToRobloxAnimation(t)
		if pose then
			pose.Parent = kf
		end
	end

	return kfs
end

function Rig:EncodeRig()
	-- Actually encoded the rig itself
	if not self.root then
		return nil
	end
	return self.root:Encode({})
end

function Rig:RebuildAsDeformRig()
	if self.isDeformRig then
		return
	end

	print("Rebuilding rig as a deform bone rig...")
	self.isDeformRig = true
	self.bones = {}
	self.bonesByInstance = {}
	if self.model.PrimaryPart then
		self.root = RigPart.new(self, self.model.PrimaryPart, nil, true)
	else
		warn("Cannot rebuild as deform rig: Model has no PrimaryPart.")
		self.root = nil
	end
	if self.root then -- Only call AddParts if root is not nil
		self:AddParts(self.root)
	end
end

function Rig:AddParts(part)
	for _, child in pairs(part.children) do
		if self.bones[child.part.Name] == nil then
			self.bones[child.part.Name] = child
		end
		self.bonesByInstance[child.part] = child
		self:AddParts(child)
	end
end

return Rig
