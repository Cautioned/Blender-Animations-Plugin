--!native
--!strict
--!optimize 2

local State = require(script.Parent.Parent.state)
local Types = require(script.Parent.Parent.types)

local BaseXX = require(script.Parent.Parent.Components.BaseXX)
local Plugin = plugin

local ExportManager = {}
ExportManager.__index = ExportManager

function ExportManager.new()
	local self = setmetatable({}, ExportManager)
	return self
end

function ExportManager:clearMetaParts()
	if State.metaParts then
		for _, metaPart in pairs(State.metaParts) do
			metaPart:Destroy()
		end
		State.metaParts = {}
	end
end

function ExportManager:reencodeJointMetadata(
	rigNode: any,
	partEncodeMap: { [Instance]: string },
	usedJointNames: { [string]: boolean }?
)
	-- Initialize usedJointNames on first call (root node)
	usedJointNames = usedJointNames or {}

	-- round transform matrices (compresses data)
	for _, transform in pairs({ "transform", "jointtransform0", "jointtransform1" }) do
		if rigNode[transform] then
			for i = 1, #rigNode[transform] do
				rigNode[transform][i] = math.floor(rigNode[transform][i] * 10000 + 0.5) / 10000
			end
		end
	end

	-- Also round auxTransform arrays (each is a 12-value CFrame)
	if rigNode.auxTransform then
		for auxIdx, auxCf in pairs(rigNode.auxTransform) do
			if auxCf and type(auxCf) == "table" then
				for i = 1, #auxCf do
					auxCf[i] = math.floor(auxCf[i] * 10000 + 0.5) / 10000
				end
			end
		end
	end

	-- Uniquify jname only for Welds (not Motor6Ds) to avoid collisions like multiple "Handle" bones
	local originalJname = rigNode.jname
	local jointType = rigNode.jointType
	local isWeld = jointType == "Weld" or jointType == "WeldConstraint"

	if originalJname and isWeld then
		local uniqueJname = originalJname
		local retryCount = 0
		while usedJointNames[uniqueJname] do
			retryCount = retryCount + 1
			uniqueJname = originalJname .. retryCount
		end
		usedJointNames[uniqueJname] = true
		rigNode.jname = uniqueJname
	elseif originalJname then
		-- Track Motor6D names too so welds don't collide with them
		usedJointNames[originalJname] = true
	end

	rigNode.pname = partEncodeMap[rigNode.inst]
	rigNode.inst = nil
	local realAux = rigNode.aux or {}
	rigNode.aux = {} -- named aux
	rigNode.auxTransform = rigNode.auxTransform or {}

	for _, aux in pairs(realAux) do
		rigNode.aux[#rigNode.aux + 1] = partEncodeMap[aux]
	end

	for _, child in pairs(rigNode.children) do
		self:reencodeJointMetadata(child, partEncodeMap, usedJointNames)
	end
end

function ExportManager:generateMetadata(rigModelToExport: Types.RigModelType, originalMap: { [Instance]: Instance })
	assert(State.activeRig)

	local partNames = {}
	local partEncodeMap = {}
	local partAuxData = {} -- New aux data for fingerprints

	local usedModelNames = {}
	local partCount = 0

	local primaryClone = rigModelToExport.PrimaryPart

	-- Iterate the CLONE's descendants.
	for descIdx, desc in ipairs(rigModelToExport:GetDescendants()) do
		if desc:IsA("BasePart") then
			partCount = partCount + 1
			local isPrimary = desc == primaryClone

			local originalInst = originalMap[desc]

			-- Verify (Optional, but good for debugging if ever needed)
			-- if originalInst and originalInst.Name ~= desc.Name then warn("Mismatch mapping?") end

			-- uniqify the name
			local baseName = desc.Name :: string
			local retryCount = 0
			while usedModelNames[desc.Name] do
				retryCount = retryCount + 1
				desc.Name = baseName .. retryCount
			end

			usedModelNames[desc.Name] = true
			if not isPrimary then
				partNames[#partNames + 1] = desc.Name
			end

			-- Generate Robust Geometric Fingerprint (Uniform Scaling)
			-- We use Uniform Scaling (modifying X, Y, Z equally) by a unique amount per ID.
			-- This guards against axis-swapping or rotation, as (X+e, Y+e, Z+e) simply scales the shape.
			-- Different IDs = Different 'e' = Distinct resulting dimensions.

			local id = partCount
			local quantum = 0.0001 -- 0.1mm steps

			local perturbation = Vector3.one * (id * quantum)
			desc.Size = desc.Size + perturbation

			-- Store Expected Dimensions/Volume
			partAuxData[partCount] = {
				idx = partCount,
				name = desc.Name,
				dims_fp = { desc.Size.X, desc.Size.Y, desc.Size.Z },
				vol_fp = desc.Size.X * desc.Size.Y * desc.Size.Z, -- Fallback for Mesh Volume calculation
			}

			if originalInst then
				partEncodeMap[originalInst] = desc.Name
			end
			desc.Name = rigModelToExport.Name .. partCount
		elseif desc:IsA("Humanoid") or desc:IsA("AnimationController") then
			-- Get rid of all humanoids so that they do not affect naming...
			desc:Destroy()
		end
	end

	local exportWelds = State.exportWelds:get()
	local encodedRig = State.activeRig:EncodeRig(exportWelds)
	if not encodedRig then
		warn("Export aborted: No encoded rig data (is the root export-disabled?).")
		return nil
	end

	self:reencodeJointMetadata(encodedRig, partEncodeMap)

	print("[ExportManager] Exporting with Robust Size Fingerprints (v1.4) - Uniform Scaling")

	return {
		rigName = State.activeRig.model.Name,
		parts = partNames,
		rig = encodedRig,
		partAux = partAuxData, -- Send aux data
		version = "1.1",
	}
end

function ExportManager:generateMetadataLegacy(
	rigModelToExport: Types.RigModelType,
	originalMap: { [Instance]: Instance }
)
	assert(State.activeRig)

	local partRoles: { [string]: string } = {} -- Maps original part names to their roles/identifiers
	local partEncodeMap: { [BasePart]: string } = {} -- Maps original parts to their roles for encoding
	local partAuxData = {}
	local usedModelNames = {}
	local partCount = 0

	local primaryClone = rigModelToExport.PrimaryPart

	for descIdx, desc in ipairs(rigModelToExport:GetDescendants()) do
		if desc:IsA("BasePart") then
			partCount = partCount + 1

			local originalInst = originalMap[desc]

			-- IMPORTANT: Uniquify names even for Legacy export to prevent collision ambiguities
			-- The user can rename them back in Blender if they really want duplicates, but for matching we need unique keys.
			local baseName = desc.Name
			local retryCount = 0
			while usedModelNames[desc.Name] do
				retryCount = retryCount + 1
				desc.Name = baseName .. retryCount
			end
			usedModelNames[desc.Name] = true

			local partRole: string = desc.Name

			if originalInst then
				partEncodeMap[originalInst] = partRole
			end
			if desc ~= primaryClone then
				partRoles[desc.Name] = partRole
			end

			-- Generate Robust Geometric Fingerprint (Legacy)
			local id = partCount
			local quantum = 0.0001

			local perturbation = Vector3.one * (id * quantum)
			desc.Size = desc.Size + perturbation

			partAuxData[partCount] = {
				idx = partCount,
				name = desc.Name,
				dims_fp = { desc.Size.X, desc.Size.Y, desc.Size.Z },
			}
		end
		-- No need to destroy Humanoid or AnimationController
	end

	local exportWelds = State.exportWelds:get()
	local encodedRig = State.activeRig:EncodeRig(exportWelds)
	if not encodedRig then
		warn("Export aborted: No encoded rig data (is the root export-disabled?).")
		return nil
	end

	self:reencodeJointMetadata(encodedRig, partEncodeMap)

	print("[ExportManager] Exporting Legacy with Size/Volume Fingerprints (v1.3)")

	return {
		rigName = State.activeRig.model.Name,
		parts = partRoles,
		rig = encodedRig,
		partAux = partAuxData,
		version = "1.1",
	}
end

function ExportManager:exportRig()
	assert(State.activeRigModel, "activeRig is nil or false")

	self:clearMetaParts()

	---- Clone the rig model, then rename all baseparts to the rig name (let the export flow handle unique indices)
	--print(activeRig)
	if State.setRigOrigin:get(true) then
		local currentCFrame = (State.activeRigModel.PrimaryPart :: BasePart).CFrame
		local newCFrame = CFrame.new(0, currentCFrame.Position.Y, 0) * CFrame.Angles(currentCFrame:ToOrientation());
		(State.activeRigModel.PrimaryPart :: BasePart).CFrame = newCFrame
	end

	local function buildArchivablePathMap(root: Instance)
		local map = {}
		local function recurse(p: Instance, path: string)
			local archivableChildren = {}
			for _, child in ipairs(p:GetChildren()) do
				if child.Archivable then
					table.insert(archivableChildren, child)
				end
			end
			for i, child in ipairs(archivableChildren) do
				local childPath = path == "" and tostring(i) or (path .. "/" .. tostring(i))
				if child:IsA("BasePart") then
					map[childPath] = child
				end
				recurse(child, childPath)
			end
		end
		recurse(root, "")
		return map
	end

	local originalPathMap = buildArchivablePathMap(State.activeRigModel)

	local wasArchivable = State.activeRigModel.Archivable
	State.activeRigModel.Archivable = true
	local rigModelToExport = State.activeRigModel:Clone()
	State.activeRigModel.Archivable = wasArchivable

	local clonePathMap = rigModelToExport and buildArchivablePathMap(rigModelToExport) or {}
	local originalMap = {}
	for path, clonePart in pairs(clonePathMap) do
		local originalPart = originalPathMap[path]
		if originalPart then
			originalMap[clonePart] = originalPart
		end
	end

	if not rigModelToExport then
		warn("[ExportManager] Failed to clone ActiveRig (Clone returned nil).")
		return
	end

	rigModelToExport.Parent = State.activeRigModel.Parent
	rigModelToExport.Archivable = false

	game.Workspace.Camera.Focus = (State.activeRigModel.PrimaryPart :: BasePart).CFrame

	State.metaParts = { rigModelToExport }

	local meta = self:generateMetadata(rigModelToExport, originalMap)
	if not meta then
		return
	end

	-- store encoded metadata in a bunch of auxiliary part names
	local metaEncodedJson = game.HttpService:JSONEncode(meta)
	local metaEncoded = BaseXX.to_base32(metaEncodedJson):gsub("=", "0")
	local idx = 1
	local segLen = 45
	for begin = 1, #metaEncoded + 1, segLen do
		local metaPart = Instance.new("Part", game.Workspace)
		metaPart.Name = ("meta%dq1%sq1"):format(idx, metaEncoded:sub(begin, begin + segLen - 1))
		State.metaParts[#State.metaParts + 1] = metaPart
		metaPart.Anchored = true
		metaPart.Archivable = false
		idx = idx + 1
	end
	game.Selection:Set(State.metaParts)
	PluginManager():ExportSelection() -- deprecated
end

function ExportManager:exportRigLegacy()
	assert(State.activeRigModel, "activeRig is nil or false")

	self:clearMetaParts()

	-- Clone the rig model, then rename all baseparts to the rig name (let the export flow handle unique indices)
	--print(activeRig)
	if State.setRigOrigin:get(true) then
		local currentCFrame = (State.activeRigModel.PrimaryPart :: BasePart).CFrame
		local newCFrame = CFrame.new(0, currentCFrame.Position.Y, 0) * CFrame.Angles(currentCFrame:ToOrientation());
		(State.activeRigModel.PrimaryPart :: BasePart).CFrame = newCFrame
	end

	local function buildArchivablePathMap(root: Instance)
		local map = {}
		local function recurse(p: Instance, path: string)
			local archivableChildren = {}
			for _, child in ipairs(p:GetChildren()) do
				if child.Archivable then
					table.insert(archivableChildren, child)
				end
			end
			for i, child in ipairs(archivableChildren) do
				local childPath = path == "" and tostring(i) or (path .. "/" .. tostring(i))
				if child:IsA("BasePart") then
					map[childPath] = child
				end
				recurse(child, childPath)
			end
		end
		recurse(root, "")
		return map
	end

	local originalPathMap = buildArchivablePathMap(State.activeRigModel)

	local wasArchivable = State.activeRigModel.Archivable
	State.activeRigModel.Archivable = true
	local rigModelToExport = State.activeRigModel:Clone()
	State.activeRigModel.Archivable = wasArchivable

	local clonePathMap = rigModelToExport and buildArchivablePathMap(rigModelToExport) or {}
	local originalMap = {}
	for path, clonePart in pairs(clonePathMap) do
		local originalPart = originalPathMap[path]
		if originalPart then
			originalMap[clonePart] = originalPart
		end
	end

	if not rigModelToExport then
		warn("[ExportManager] Failed to clone ActiveRig (Clone returned nil).")
		return
	end

	rigModelToExport.Parent = State.activeRigModel.Parent
	rigModelToExport.Archivable = false

	game.Workspace.Camera.Focus = (State.activeRigModel.PrimaryPart :: BasePart).CFrame

	State.metaParts = { rigModelToExport }

	local meta = self:generateMetadataLegacy(rigModelToExport, originalMap)
	if not meta then
		return
	end

	-- store encoded metadata in a bunch of auxiliary part names
	local metaEncodedJson = game.HttpService:JSONEncode(meta)
	local metaEncoded = BaseXX.to_base32(metaEncodedJson):gsub("=", "0")
	local idx = 1
	local segLen = 45
	for begin = 1, #metaEncoded + 1, segLen do
		local metaPart = Instance.new("Part", game.Workspace)
		metaPart.Name = ("meta%dq1%sq1"):format(idx, metaEncoded:sub(begin, begin + segLen - 1))
		State.metaParts[#State.metaParts + 1] = metaPart
		metaPart.Anchored = true
		metaPart.Archivable = false
		idx = idx + 1
	end
	game.Selection:Set(State.metaParts)
	PluginManager():ExportSelection() -- deprecated
end

return ExportManager
