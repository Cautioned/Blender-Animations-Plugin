--!native
--!strict
--!optimize 2

local State = require(script.Parent.Parent.state)
local Types = require(script.Parent.Parent.types)

local Rig = require(script.Parent.Parent.Components.Rig)

local RigManager = {}
RigManager.__index = RigManager

local EXTREME_DEPTH_LIMIT = 512 -- allow very deep rigs before triggering overflow guard

function RigManager.new(playbackService: any, cameraManager: any?)
	local self = setmetatable({}, RigManager)
	
	self.playbackService = playbackService
	self.cameraManager = cameraManager
	self.boneWeights = {} :: Types.BoneWeightsList
	
	return self
end

-- Function to check if the selected object is a valid rig
function RigManager:isValidRig(object: any): boolean
	return typeof(object) == "Instance"
		and object:IsA("Model")
		and (object:FindFirstChildWhichIsA("Humanoid") or object:FindFirstChildWhichIsA("AnimationController"))
			~= nil
end

function RigManager:isKeyframeSequence(object: any): boolean
	return typeof(object) == "Instance" and object:IsA("KeyframeSequence")
end

-- Extract warning checks to reduce function complexity
function RigManager:addRigWarnings(rigModel: Types.RigModelType)
	if rigModel.PrimaryPart and (rigModel.PrimaryPart :: BasePart).Name == "Head" then
		self:addWarning(
			"PrimaryPart is set to the Head, which may cause issues exporting. \n If this is an R6 Rig, please set it to HumanoidRootPart before exporting."
		)
	end

	if rigModel.PrimaryPart and not (rigModel.PrimaryPart :: BasePart).Anchored then
		self:addWarning("Rig PrimaryPart is not anchored!")
	end

	local success, settingsResult = pcall(function() return settings() end)
	if success and settingsResult and settingsResult.Rendering and settingsResult.Rendering.ExportMergeByMaterial then
		self:addWarning("Warning: ExportMergeByMaterial should be disabled! Disable under Studio settings -> Rendering")
	end

	if rigModel.Name:match("[^a-zA-Z0-9]") then
		self:addWarning("Warning: Model name should only contain a-z A Z 0-9 symbols!")
	end
end

function RigManager:addWarning(newWarning)
	local currentWarnings = State.activeWarnings:get()
	table.insert(currentWarnings, newWarning)
	State.activeWarnings:set(currentWarnings)
end

function RigManager:clearWarnings()
	State.activeWarnings:set({})
end

function RigManager:rebuildBoneWeights()
	if not State.activeRig or not State.activeRig.root then
		self.boneWeights = {}
		State.boneWeights:set({})
		State.exportBoneWeights:set({})
		return
	end

	-- Determine which bones to show: only enabled bones and their ancestors
	local bonesToShow
	local hasAnimatedBones = false
	for _, bone in pairs(State.activeRig.bones) do
		if bone.enabled then
			hasAnimatedBones = true
			break
		end
	end

	if hasAnimatedBones then
		bonesToShow = {}
		-- Iterative post-order traversal with cycle guard
		local stack = { { node = State.activeRig.root, depth = 0, visited = false } }
		local visited = {}
		local hasEnabled: { [any]: boolean } = {}
		local processed = 0
		while #stack > 0 do
			local current = stack[#stack]
			stack[#stack] = nil
			local node = current.node
			local depth = current.depth
			if current.visited then
				local isOrHasEnabledDescendant = node.enabled
				for _, child in ipairs(node.children) do
					if hasEnabled[child] then
						isOrHasEnabledDescendant = true
					end
				end
				if isOrHasEnabledDescendant then
					bonesToShow[node] = true
				end
				hasEnabled[node] = isOrHasEnabledDescendant
			else
				if visited[node] then
					continue
				end
				visited[node] = true
				processed += 1
				if processed > EXTREME_DEPTH_LIMIT * 8 then
					warn("Bone traversal exceeded safe node count; stopping at:", node.part.Name)
					break
				end
				if depth > EXTREME_DEPTH_LIMIT then
					warn("Bone hierarchy too deep (>", EXTREME_DEPTH_LIMIT, " levels), stopping traversal at:", node.part.Name)
					continue
				end
				-- Post-order: push marker, then children
				stack[#stack + 1] = { node = node, depth = depth, visited = true }
				for _, child in ipairs(node.children) do
					stack[#stack + 1] = { node = child, depth = depth + 1, visited = false }
				end
			end
		end
	end

	-- Build the list for the UI with a single traversal
	local boneList: Types.BoneWeightsList = {}
	local exportBoneList: Types.ExportBoneWeightsList = {}
	local function buildBoneListIterative(root)
		local stack = { { node = root, depth = 0 } }
		local visited = {}
		local processed = 0
		while #stack > 0 do
			local current = stack[#stack]
			stack[#stack] = nil
			local node = current.node
			local depth = current.depth
			if visited[node] then
				continue
			end
			visited[node] = true
			processed += 1
			if processed > EXTREME_DEPTH_LIMIT * 8 then
				warn("Bone traversal exceeded safe node count; stopping at:", node.part.Name)
				break
			end
			if depth > EXTREME_DEPTH_LIMIT then
				warn("Bone hierarchy too deep (>", EXTREME_DEPTH_LIMIT, " levels), stopping traversal at:", node.part.Name)
				continue
			end
			for _, child in ipairs(node.children) do
				if child.jointType == "Weld" or child.jointType == "WeldConstraint" then
					-- Skip welds in bone toggles
					continue
				end
				if not hasAnimatedBones or (bonesToShow and bonesToShow[child]) then
					table.insert(boneList, {
						name = child.part.Name,
						enabled = child.enabled,
						depth = depth,
						parentName = node.part.Name,
					})
				end
				stack[#stack + 1] = { node = child, depth = depth + 1 }
			end
		end
	end

	local function buildExportBoneListIterative(root)
		local stack = { { node = root, depth = 0 } }
		local visited = {}
		local processed = 0
		while #stack > 0 do
			local current = stack[#stack]
			stack[#stack] = nil
			local node = current.node
			local depth = current.depth
			if visited[node] then
				continue
			end
			visited[node] = true
			processed += 1
			if processed > EXTREME_DEPTH_LIMIT * 8 then
				warn("Export bone traversal exceeded safe node count; stopping at:", node.part.Name)
				break
			end
			if depth > EXTREME_DEPTH_LIMIT then
				warn("Bone hierarchy too deep (>", EXTREME_DEPTH_LIMIT, " levels), stopping traversal at:", node.part.Name)
				continue
			end
			for _, child in ipairs(node.children) do
				if child.jointType == "Weld" or child.jointType == "WeldConstraint" then
					-- Skip welds in export toggles
					continue
				end
				table.insert(exportBoneList, {
					name = child.part.Name,
					enabled = child.exportEnabled ~= false,
					depth = depth,
					parentName = node.part.Name,
				})
				stack[#stack + 1] = { node = child, depth = depth + 1 }
			end
		end
	end

	buildBoneListIterative(State.activeRig.root)
	buildExportBoneListIterative(State.activeRig.root)
	self.boneWeights = boneList
	State.boneWeights:set(boneList)
	State.exportBoneWeights:set(exportBoneList)
end

function RigManager:updatePartsList()
	if not State.activeRig then
		-- State.rigPartsList:set({}) -- This needs to be handled by the main file
		return
	end

	local newParts = {}
	for _, rigPart in pairs(State.activeRig.bones) do
		if rigPart.part:IsA("BasePart") then
			table.insert(newParts, rigPart.part.Name)
		end
	end

	table.sort(newParts, function(a, b)
		local aLower = a:lower()
		local bLower = b:lower()
		local aIsMatch = string.find(aLower, "head") or string.find(aLower, "camera")
		local bIsMatch = string.find(bLower, "head") or string.find(bLower, "camera")
		if aIsMatch and not bIsMatch then
			return true
		elseif not aIsMatch and bIsMatch then
			return false
		else
			return a < b -- Alphabetical sort for same-category items
		end
	end)

	-- State.rigPartsList:set(newParts) -- This needs to be handled by the main file
end

function RigManager:updateSavedAnimationsList()
	if not State.activeRigModel then
		State.savedAnimations:set({})
		return
	end

	local animSaves = State.activeRigModel:FindFirstChild("AnimSaves")
	if not animSaves then
		State.savedAnimations:set({})
		return
	end

	local animations = {}
	for _, anim in ipairs(animSaves:GetChildren()) do
		if anim:IsA("KeyframeSequence") or anim:IsA("CurveAnimation") then
			table.insert(animations, {
				name = anim.Name,
				instance = anim,
				type = anim.ClassName,
			})
		end
	end

	State.savedAnimations:set(animations)
end

function RigManager:toggleBone(name)
	local currentWeights = self.boneWeights
	if not currentWeights then
		return
	end

	for _, bone in ipairs(currentWeights) do
		if bone.name == name then
			bone.enabled = not bone.enabled
			-- Update the State
			State.boneWeights:set(currentWeights)
			-- Update the rig animation
			if State.activeRig and State.activeRig.bones then
				for _, rigBone in pairs(State.activeRig.bones) do
					if rigBone.part.Name == name then
						rigBone.enabled = bone.enabled
						break
					end
				end
			end
			break
		end
	end
	self.boneWeights = currentWeights
	self.playbackService:stopAnimationAndDisconnect()
	self.playbackService:playCurrentAnimation(State.activeAnimator)
end

-- Main rig setting function
function RigManager:setRig(rigModel: Types.RigModelType?): any
	if State.lastSelectionWasKeyframeSequence then
		State.lastSelectionWasKeyframeSequence = false
		return
	end

	State.loadingEnabled:set(true)
	self:clearWarnings()

	local previousAnimator = State.activeAnimator
	local previousRigModel = State.activeRigModel
	
	if rigModel == nil then
		-- No new rig selected; keep playing on previous animator if it exists
		State.loadingEnabled:set(false)
		return
	end

	if previousRigModel and previousRigModel ~= rigModel then
		if previousAnimator then
			self.playbackService:stopAnimationAndDisconnect({
				background = true,
				animatorOverride = previousAnimator,
			})
		end
	end

	-- explicitly stop current animation before switching to the new rig
	self.playbackService:stopAnimationAndDisconnect()

	if not rigModel then
		State.activeRigModel = nil
		State.activeAnimator = nil
		State.rigModelName:set("No Rig Selected")
		State.activeRigExists:set(false)
		State.loadingEnabled:set(false)
		return
	end

	if not rigModel.PrimaryPart then
		self:addWarning("Rig has no PrimaryPart set.")
		if rigModel:FindFirstChild("HumanoidRootPart") then
			rigModel.PrimaryPart = rigModel:FindFirstChild("HumanoidRootPart") :: BasePart
		else
			return
		end
	end

	State.activeRigModel = rigModel
	State.activeAnimator = (
		rigModel:FindFirstChildWhichIsA("Humanoid") or rigModel:FindFirstChildWhichIsA("AnimationController")
	) :: any

	if not State.activeAnimator then
		self:addWarning("Invalid rig, no Humanoid or AnimationController exists.")
		State.activeRigModel = nil
		State.activeRig = nil
		State.rigModelName:set("No Rig Selected")
		State.activeRigExists:set(false)
		return
	end

	-- Check model size to prevent performance issues
	local descendantCount = 0
	for _, descendant in pairs(rigModel:GetDescendants()) do
		if descendant:IsA("Part") then
			descendantCount = descendantCount + 1
			if descendantCount > 5000 then -- Prevent performance issues with huge models
				self:addWarning("Model has too many parts (" .. descendantCount .. "). This may cause performance issues.")
				break
			end
		end
	end

	-- Use extracted warning function
	self:addRigWarnings(rigModel)

	assert(State.activeRigModel)
	State.rigScale:set(State.activeRigModel:GetScale())
	State.rigModelName:set(rigModel.Name)
	State.activeRigExists:set(true)

	-- Use the old pattern: Rig.new(rigModel :: any) :: any
	local success, result = pcall(function()
		-- Add timeout protection for rig creation
		local startTime = tick()
		local rig = Rig.new(rigModel :: any) :: any
		local elapsed = tick() - startTime
		
		if elapsed > 5 then -- Warn if rig creation takes too long
			warn("Rig creation took " .. elapsed .. " seconds. This may indicate performance issues.")
		end
		
		return rig
	end)
	
		if success then
			State.activeRig = result
		else
			-- Check if it's a circular dependency warning
			if string.find(tostring(result), "CIRCULAR MOTOR6D CHAIN DETECTED") then
				local warningMsg = tostring(result)
				self:addWarning(warningMsg)
				print("RIG MANAGER:", warningMsg)
			else
				local errorMsg = "Failed to create rig: " .. tostring(result)
				self:addWarning(errorMsg)
				print("RIG MANAGER:", errorMsg)
			end
			State.activeRig = nil
			State.activeRigExists:set(false)
			return
		end

	-- The caller `updateActiveRigFromSelection` runs this in a `task.spawn`.
	-- The functions below have internal cache checks to avoid re-computing.
	self:rebuildBoneWeights()
	self:updatePartsList()
	self:updateSavedAnimationsList()
	
	-- Update camera manager parts list if available
	if self.cameraManager then
		self.cameraManager:updatePartsList()
	end

	State.loadingEnabled:set(false)

	return true
end

function RigManager:cleanup()
	State.loadingEnabled:set(false)
	State.rigModelName:set("No Rig Selected")
	State.activeRigModel = nil
	State.activeAnimator = nil
	State.activeRig = nil
	State.activeRigExists:set(false)
	State.boneWeights:set({})
	self:clearWarnings()
	self.boneWeights = {}
end

-- Sync missing bones from the selected Blender armature into the current Studio rig.
-- If the active rig is Motor6D-based, creates a placeholder Part and a Motor6D under the resolved parent part.
-- If it's a deform-bone rig, creates a Bone under the most appropriate mesh/bone parent.
function RigManager:syncBones(blenderSyncManager: any?): boolean
    if not State.activeRigModel or not State.activeRig then
        print("Sync Bones: No active rig to sync")
        return false
    end

    -- check if server is connected
    if not State.isServerConnected:get() then
        print("Sync Bones: Not connected to Blender server. Please connect in the Blender Sync tab first.")
        return false
    end

    -- obtain armature info from blender server
    local connectionService: any? = nil
    if blenderSyncManager and blenderSyncManager.blenderConnectionService then
        connectionService = blenderSyncManager.blenderConnectionService
    else
        -- lazy import to avoid hard dependency if not provided
        local ok, BlenderConnection = pcall(function()
            return require(script.Parent.Parent.Components.BlenderConnection)
        end)
        if ok and BlenderConnection then
            connectionService = BlenderConnection.new(game:GetService("HttpService"))
        end
    end
    if not connectionService then
        print("Sync Bones: Cannot create connection service")
        return false
    end

    print("Sync Bones: Fetching armatures from Blender server...")
    local armatures = connectionService:ListArmatures(State.serverPort:get())
    if not armatures or #armatures == 0 then
        print("Sync Bones: No armatures found in Blender. Make sure you have an armature object in your Blender scene.")
        return false
    end

    -- choose armature: prefer explicitly selected one, else try to match rig model name
    local targetArmatureName: string? = nil
    local selectedArmature = State.selectedArmature:get()
    print("Sync Bones: selectedArmature =", selectedArmature)
    if selectedArmature and (selectedArmature :: any).name then
        targetArmatureName = (selectedArmature :: any).name
        print("Sync Bones: targetArmatureName from selected =", targetArmatureName)
    else
        targetArmatureName = State.activeRigModel.Name
        print("Sync Bones: targetArmatureName from rig model =", targetArmatureName)
    end

    print("Sync Bones: armatures =", armatures)
    for i, a in ipairs(armatures) do
        print("Sync Bones: armature[" .. i .. "] =", a, "name =", a.name)
    end

    local armatureInfo: any? = nil
    for _, a in ipairs(armatures) do
        print("Sync Bones: checking if '" .. a.name .. "' == '" .. (targetArmatureName or "nil") .. "'")
        if a.name == targetArmatureName then
            armatureInfo = a
            print("Sync Bones: found matching armature!")
            break
        end
    end
    if not armatureInfo then
        -- fallback: first armature
        armatureInfo = armatures[1]
        print("Sync Bones: Using fallback armature '" .. (armatureInfo and armatureInfo.name or "nil") .. "' (selected armature '" .. (targetArmatureName or "none") .. "' not found)")
    end
    if not armatureInfo then
        print("Sync Bones: Unable to resolve a Blender armature to sync against")
        return false
    end

    print("Sync Bones: Using armature '" .. (armatureInfo.name or "nil") .. "' with " .. (armatureInfo.num_bones or "nil") .. " bones")

    -- fetch rest transforms for c0/bone cframe
    local restData = connectionService:GetBoneRest(State.serverPort:get(), armatureInfo.name)
    local bonePoses = restData and restData.bone_poses or nil
    if bonePoses then
        print("Sync Bones: Retrieved rest transforms from Blender")
    else
        print("Sync Bones: Rest transforms unavailable; defaulting to identity")
    end

    local bonesList = armatureInfo.bones or {}
    local hierarchy = armatureInfo.bone_hierarchy or {}

    -- build a fast lookup of existing rig bone/part names
    local existing: { [string]: boolean } = {}
    for name, _ in pairs(State.activeRig.bones) do
        existing[name] = true
    end

    local createdCount = 0
    for _, boneName in ipairs(bonesList) do
        if not existing[boneName] then
            local parentName: string? = hierarchy and hierarchy[boneName] or nil
            local parentRigPart: any? = parentName and State.activeRig.bones[parentName] or nil

            if State.activeRig.isDeformRig then
                -- create a Bone
                local parentBone: Bone? = nil
                if parentRigPart and parentRigPart.bone then
                    parentBone = parentRigPart.bone :: Bone
                end

                -- find a suitable mesh parent
                local meshParent: Instance? = nil
                if parentBone then
                    meshParent = parentBone.Parent
                end
                if not meshParent then
                    for _, d in ipairs(State.activeRigModel:GetDescendants()) do
                        if d:IsA("Bone") then
                            meshParent = d.Parent
                            break
                        end
                    end
                end
                if not meshParent then
                    for _, d in ipairs(State.activeRigModel:GetDescendants()) do
                        if d:IsA("MeshPart") then
                            meshParent = d
                            break
                        end
                    end
                end

                if meshParent then
                    local newBone = Instance.new("Bone")
                    newBone.Name = boneName
                    local poseData = bonePoses and bonePoses[boneName]
                    if poseData and poseData.relative and type(poseData.relative) == "table" and #poseData.relative >= 12 then
                        newBone.CFrame = CFrame.new(unpack(poseData.relative))
                    else
                        newBone.CFrame = CFrame.new()
                    end
                    if parentBone then
                        newBone.Parent = parentBone
                    else
                        newBone.Parent = meshParent
                    end
                    print("Sync Bones: Created Bone '" .. boneName .. "' under '" .. meshParent.Name .. "'")
                    createdCount += 1
                else
                    print("Sync Bones: No suitable mesh parent found for bone '" .. boneName .. "'; skipping")
                end
            else
                -- create Motor6D + placeholder Part
                local parentPart: BasePart? = nil
                if parentRigPart and parentRigPart.part and parentRigPart.part:IsA("BasePart") then
                    parentPart = parentRigPart.part :: BasePart
                end
                if not parentPart then
                    local hand = State.activeRigModel:FindFirstChild("RightHand", true)
                        or State.activeRigModel:FindFirstChild("LeftHand", true)
                    if hand and hand:IsA("BasePart") then
                        parentPart = hand
                    else
                        parentPart = State.activeRigModel.PrimaryPart
                    end
                end

                if parentPart then
                    local newPart = Instance.new("Part")
                    newPart.Name = boneName
                    newPart.Size = Vector3.new(0.25, 0.25, 0.25)
                    newPart.Massless = true
                    newPart.Anchored = false
                    newPart.CanCollide = false
                    newPart.CanQuery = false
                    newPart.CanTouch = false
                    newPart.Transparency = 0.5
                    pcall(function()
                        newPart.Color = Color3.fromRGB(255, 170, 0)
                    end)
                    newPart.Parent = State.activeRigModel
                    local relativeTransform = CFrame.new(0, 0, -0.5)
                    local poseData = bonePoses and bonePoses[boneName]
                    if poseData and poseData.relative and type(poseData.relative) == "table" and #poseData.relative >= 12 then
                        -- Use the relative transform directly (deform bone transformations already applied on Blender side)
                        relativeTransform = CFrame.new(unpack(poseData.relative))
                    end
                    newPart.CFrame = (parentPart :: BasePart).CFrame * relativeTransform

                    -- Add highlight to make the placeholder part more visible
                    local highlight = Instance.new("Highlight")
                    highlight.FillColor = Color3.fromRGB(255, 255, 0) -- Bright yellow
                    highlight.OutlineColor = Color3.fromRGB(255, 0, 0) -- Red outline
                    highlight.FillTransparency = 0.3
                    highlight.OutlineTransparency = 0
                    highlight.Parent = newPart

                    local m6d = Instance.new("Motor6D")
                    m6d.Name = boneName
                    m6d.Part0 = parentPart
                    m6d.Part1 = newPart
                    m6d.C0 = relativeTransform
                    m6d.C1 = CFrame.new()
                    m6d.Parent = parentPart
                    createdCount += 1
                else
                    print("Sync Bones: No suitable parent part found for motor6d '" .. boneName .. "'; skipping")
                end
            end
        end
    end

    if createdCount > 0 then
        print("Sync Bones: Successfully created " .. createdCount .. " new bone(s). Rebuilding rig...")
        -- rebuild rig to include newly created nodes
        self:setRig(State.activeRigModel)
        
        -- restart animation to reflect changes
        self.playbackService:stopAnimationAndDisconnect()
        if State.activeAnimator then
            self.playbackService:playCurrentAnimation(State.activeAnimator)
        end
        return true
    else
        print("Sync Bones: No new bones needed to be created - all bones from Blender armature already exist in Studio rig")
    end

    return false
end

return RigManager
