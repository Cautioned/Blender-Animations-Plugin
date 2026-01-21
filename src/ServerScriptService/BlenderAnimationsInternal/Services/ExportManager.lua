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

function ExportManager:reencodeJointMetadata(rigNode: any, partEncodeMap: { [Instance]: string }, usedJointNames: { [string]: boolean }?)
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
	local realAux = rigNode.aux
	rigNode.aux = {} -- named aux
	rigNode.auxTransform = rigNode.auxTransform or {}

	for _, aux in pairs(realAux) do
		rigNode.aux[#rigNode.aux + 1] = partEncodeMap[aux]
	end

	for _, child in pairs(rigNode.children) do
		self:reencodeJointMetadata(child, partEncodeMap, usedJointNames)
	end
end

function ExportManager:generateMetadata(rigModelToExport: Types.RigModelType)
	assert(State.activeRig)

	local partNames = {}
	local partEncodeMap = {}

	local usedModelNames = {}
	local partCount = 0

	local originalDescendants = State.activeRig.model:GetDescendants()
	local primaryClone = rigModelToExport.PrimaryPart
	for descIdx, desc in ipairs(rigModelToExport:GetDescendants()) do
		if desc:IsA("BasePart") then
			partCount = partCount + 1
			local isPrimary = desc == primaryClone

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
			partEncodeMap[originalDescendants[descIdx]] = desc.Name
			desc.Name = rigModelToExport.Name .. partCount
		elseif desc:IsA("Humanoid") or desc:IsA("AnimationController") then
			-- Get rid of all humanoids so that they do not affect naming...
			desc:Destroy()
		end
	end

	local encodedRig = State.activeRig:EncodeRig()
	if not encodedRig then
		warn("Export aborted: No encoded rig data (is the root export-disabled?).")
		return nil
	end

	self:reencodeJointMetadata(encodedRig, partEncodeMap)

	return { rigName = State.activeRig.model.Name, parts = partNames, rig = encodedRig }
end

function ExportManager:generateMetadataLegacy(rigModelToExport: Types.RigModelType)
	assert(State.activeRig)

	local partRoles: { [string]: string } = {} -- Maps original part names to their roles/identifiers
	local partEncodeMap: { [BasePart]: string } = {} -- Maps original parts to their roles for encoding
	local primaryClone = rigModelToExport.PrimaryPart

	for _, desc in ipairs(rigModelToExport:GetDescendants()) do
		if desc:IsA("BasePart") then
			local partRole: string = desc.Name -- or any logic to determine the part's role/identifier
			partEncodeMap[desc :: BasePart] = partRole
			if desc ~= primaryClone then
				partRoles[desc.Name] = partRole
			end
		end
		-- No need to destroy Humanoid or AnimationController
	end

	local encodedRig = State.activeRig:EncodeRig()
	if not encodedRig then
		warn("Export aborted: No encoded rig data (is the root export-disabled?).")
		return nil
	end

	self:reencodeJointMetadata(encodedRig, partEncodeMap)

	return { rigName = State.activeRig.model.Name, parts = partRoles, rig = encodedRig }
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

	local rigModelToExport = State.activeRigModel:Clone()
	rigModelToExport.Parent = State.activeRigModel.Parent
	rigModelToExport.Archivable = false

	game.Workspace.Camera.Focus = (State.activeRigModel.PrimaryPart :: BasePart).CFrame

	State.metaParts = { rigModelToExport }

	local meta = self:generateMetadata(rigModelToExport)
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
	PluginManager():ExportSelection(); -- deprecated
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

	local rigModelToExport = State.activeRigModel:Clone()
	rigModelToExport.Parent = State.activeRigModel.Parent
	rigModelToExport.Archivable = false

	game.Workspace.Camera.Focus = (State.activeRigModel.PrimaryPart :: BasePart).CFrame

	State.metaParts = { rigModelToExport }

	local meta = self:generateMetadataLegacy(rigModelToExport)
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
	PluginManager():ExportSelection(); -- deprecated
end

return ExportManager
