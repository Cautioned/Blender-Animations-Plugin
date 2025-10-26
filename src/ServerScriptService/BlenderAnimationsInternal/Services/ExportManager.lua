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

function ExportManager:reencodeJointMetadata(rigNode: any, partEncodeMap: { [Instance]: string })
	-- round transform matrices (compresses data)
	for _, transform in pairs({ "transform", "jointtransform0", "jointtransform1" }) do
		if rigNode[transform] then
			for i = 1, #rigNode[transform] do
				rigNode[transform][i] = math.floor(rigNode[transform][i] * 10000 + 0.5) / 10000
			end
		end
	end

	rigNode.pname = partEncodeMap[rigNode.inst]
	rigNode.inst = nil
	local realAux = rigNode.aux
	rigNode.aux = {} -- named aux

	for _, aux in pairs(realAux) do
		rigNode.aux[#rigNode.aux + 1] = partEncodeMap[aux]
	end

	for _, child in pairs(rigNode.children) do
		self:reencodeJointMetadata(child, partEncodeMap)
	end
end

function ExportManager:generateMetadata(rigModelToExport: Types.RigModelType)
	assert(State.activeRig)

	local partNames = {}
	local partEncodeMap = {}

	local usedModelNames = {}
	local partCount = 0

	local originalDescendants = State.activeRig.model:GetDescendants()
	for descIdx, desc in ipairs(rigModelToExport:GetDescendants()) do
		if desc:IsA("BasePart") then
			partCount = partCount + 1

			-- uniqify the name
			local baseName = desc.Name :: string
			local retryCount = 0
			while usedModelNames[desc.Name] do
				retryCount = retryCount + 1
				desc.Name = baseName .. retryCount
			end

			usedModelNames[desc.Name] = true
			partNames[#partNames + 1] = desc.Name
			partEncodeMap[originalDescendants[descIdx]] = desc.Name
			desc.Name = rigModelToExport.Name .. partCount
		elseif desc:IsA("Humanoid") or desc:IsA("AnimationController") then
			-- Get rid of all humanoids so that they do not affect naming...
			desc:Destroy()
		end
	end

	local encodedRig = State.activeRig:EncodeRig()
	self:reencodeJointMetadata(encodedRig, partEncodeMap)

	return { rigName = State.activeRig.model.Name, parts = partNames, rig = encodedRig }
end

function ExportManager:generateMetadataLegacy(rigModelToExport: Types.RigModelType)
	assert(State.activeRig)

	local partRoles: { [string]: string } = {} -- Maps original part names to their roles/identifiers
	local partEncodeMap: { [BasePart]: string } = {} -- Maps original parts to their roles for encoding

	for _, desc in ipairs(rigModelToExport:GetDescendants()) do
		if desc:IsA("BasePart") then
			local partRole: string = desc.Name -- or any logic to determine the part's role/identifier
			partRoles[desc.Name] = partRole
			partEncodeMap[desc :: BasePart] = partRole
		end
		-- No need to destroy Humanoid or AnimationController
	end

	local encodedRig = State.activeRig:EncodeRig()

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
