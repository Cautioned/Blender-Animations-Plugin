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

	-- Iterate the CLONE's descendants.
	for descIdx, desc in ipairs(rigModelToExport:GetDescendants()) do
		if desc:IsA("BasePart") then
			partCount = partCount + 1

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
			-- Include ALL parts (including primary) so partNames indices stay
			-- aligned with partCount — blender's autoname_parts does
			-- partnames[index-1] where index comes from the renamed mesh name.
			partNames[#partNames + 1] = desc.Name

			-- Generate Robust Geometric Fingerprint (Uniform Scaling)
			-- We use Uniform Scaling (modifying X, Y, Z equally) by a unique amount per ID.
			-- This guards against axis-swapping or rotation, as (X+e, Y+e, Z+e) simply scales the shape.
			-- Different IDs = Different 'e' = Distinct resulting dimensions.

			local id = partCount
			local quantum = 0.0001 -- 1cm steps - must survive FBX precision loss

			local perturbation = Vector3.one * (id * quantum)
			desc.Size = desc.Size + perturbation

			-- Store Expected Dimensions/Volume (use table.insert for proper JSON array serialization)
			table.insert(partAuxData, {
				idx = partCount,
				name = desc.Name,
				dims_fp = { desc.Size.X, desc.Size.Y, desc.Size.Z },
				vol_fp = desc.Size.X * desc.Size.Y * desc.Size.Z, -- Fallback for Mesh Volume calculation
			})

			if originalInst then
				partEncodeMap[originalInst] = desc.Name
			end
			-- Use unambiguous "p<N>x" naming. The alphabetic delimiters avoid
			-- regex ambiguity when rigName ends with digits, and survive OBJ
			-- import mangling (group suffix "1", case changes, dedup suffixes).
			desc.Name = ("p%dx"):format(partCount)
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

	return {
		rigName = State.activeRig.model.Name,
		parts = partNames,
		rig = encodedRig,
		partAux = partAuxData,
		version = "1.2",
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
			local quantum = 0.0001 -- 1cm steps - must survive FBX precision loss

			local perturbation = Vector3.one * (id * quantum)
			desc.Size = desc.Size + perturbation

			-- use table.insert for proper JSON array serialization
			table.insert(partAuxData, {
				idx = partCount,
				name = desc.Name,
				dims_fp = { desc.Size.X, desc.Size.Y, desc.Size.Z },
			})
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

	-- print("[ExportManager] Exporting Legacy with Size/Volume Fingerprints (v1.3)")

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
	
	-- Wait a frame for size changes to propagate before export
	task.wait()
	
	game.Selection:Set(State.metaParts)
	PluginManager():ExportSelection() -- deprecated
end

-- Resolve weapon container + root from an Instance (Tool/Model/Accoutrement/BasePart).
-- Returns (container, rootPart) or (nil, nil).
function ExportManager:resolveWeapon(inst: Instance): (Instance?, BasePart?)
	local weaponContainer: Instance? = nil
	local weaponRoot: BasePart? = nil

	if inst:IsA("Tool") or inst:IsA("Accoutrement") or inst:IsA("Model") then
		weaponContainer = inst
	elseif inst:IsA("BasePart") then
		-- walk up to find container, but stop if we hit the active rig or workspace/game
		local rigModel = State.activeRigModel or State.lastKnownRigModel
		local parent = inst.Parent
		while parent and not parent:IsA("WorldRoot") and not (parent:IsA("Tool") or parent:IsA("Model") or parent:IsA("Accoutrement")) do
			parent = parent.Parent
		end
		if parent and not parent:IsA("WorldRoot") and (parent:IsA("Tool") or parent:IsA("Model") or parent:IsA("Accoutrement")) and parent ~= rigModel then
			weaponContainer = parent
		else
			-- bare part/mesh with no suitable container — use it directly
			weaponContainer = nil
			weaponRoot = inst :: BasePart
		end
	end

	if weaponContainer and not weaponRoot then
		-- auto-detect root priority:
		-- 1. the part that connects to the active rig (the grip/attachment point)
		-- 2. PrimaryPart
		-- 3. most-connected hub
		local rigModel = State.activeRigModel or State.lastKnownRigModel

		-- 1. find the weapon part connected to the rig
		if rigModel then
			local weaponPartSet: { [Instance]: boolean } = {}
			for _, desc in ipairs(weaponContainer:GetDescendants()) do
				if desc:IsA("BasePart") then
					weaponPartSet[desc] = true
				end
			end
			for _, desc in ipairs(rigModel:GetDescendants()) do
				if desc:IsA("Motor6D") or desc:IsA("Weld") or desc:IsA("WeldConstraint") then
					local j = desc :: any
					local p0, p1 = j.Part0, j.Part1
					if not p0 or not p1 then continue end
					if weaponPartSet[p1] and p0:IsDescendantOf(rigModel) and not weaponPartSet[p0] then
						weaponRoot = p1 :: BasePart
						break
					elseif weaponPartSet[p0] and p1:IsDescendantOf(rigModel) and not weaponPartSet[p0] then
						weaponRoot = p0 :: BasePart
						break
					end
				end
			end
			-- also check joints inside the weapon container
			if not weaponRoot then
				for _, desc in ipairs(weaponContainer:GetDescendants()) do
					if desc:IsA("Motor6D") or desc:IsA("Weld") or desc:IsA("WeldConstraint") then
						local j = desc :: any
						local p0, p1 = j.Part0, j.Part1
						if not p0 or not p1 then continue end
						if weaponPartSet[p1] and p0:IsDescendantOf(rigModel) and not weaponPartSet[p0] then
							weaponRoot = p1 :: BasePart
							break
						elseif weaponPartSet[p0] and p1:IsDescendantOf(rigModel) and not weaponPartSet[p0] then
							weaponRoot = p0 :: BasePart
							break
						end
					end
				end
			end
		end

		-- 2. fall back to PrimaryPart
		if not weaponRoot and weaponContainer:IsA("Model") and (weaponContainer :: Model).PrimaryPart then
			weaponRoot = (weaponContainer :: Model).PrimaryPart
		end

		-- 3. fall back to most-connected hub
		if not weaponRoot then
			local bestPart: BasePart? = nil
			local bestCount = -1
			for _, desc in ipairs(weaponContainer:GetDescendants()) do
				if desc:IsA("BasePart") then
					local count = 0
					for _, child in ipairs(desc:GetChildren()) do
						if child:IsA("Motor6D") or child:IsA("Weld") then
							count += 1
						end
					end
					for _, d2 in ipairs(weaponContainer:GetDescendants()) do
						if (d2:IsA("Motor6D") or d2:IsA("Weld")) and d2.Parent ~= desc then
							if (d2 :: any).Part0 == desc or (d2 :: any).Part1 == desc then
								count += 1
							end
						end
					end
					if count > bestCount then
						bestCount = count
						bestPart = desc :: BasePart
					end
				end
			end
			weaponRoot = bestPart
		end
	end

	return weaponContainer, weaponRoot
end

-- Slot the current studio selection as the weapon.
-- Updates State.selectedWeapon / selectedWeaponName and auto-detects connection.
function ExportManager:pickWeapon()
	local selection = game.Selection:Get()
	if #selection == 0 then
		warn("[ExportManager] Nothing selected. Select a Tool, Model, Accessory, or Part.")
		return
	end

	local inst = selection[1]
	local container, root = self:resolveWeapon(inst)

	if not root then
		warn("[ExportManager] Selected object is not a valid weapon (no BaseParts found).")
		return
	end

	local name = container and container.Name or root.Name
	State.selectedWeapon:set(container or root)
	State.selectedWeaponName:set(name)
	print(("[ExportManager] Weapon slotted: %s"):format(name))

	self:detectWeaponConnection()
end

-- Clear the slotted weapon.
function ExportManager:clearWeapon()
	State.selectedWeapon:set(nil)
	State.selectedWeaponName:set("No Weapon Selected")
	State.weaponConnectionStatus:set("")
end

-- Detect whether the slotted weapon has a joint connection to the active rig.
-- Updates State.weaponConnectionStatus reactively.
function ExportManager:detectWeaponConnection()
	local weapon = State.selectedWeapon:get()
	if not weapon then
		State.weaponConnectionStatus:set("")
		return
	end

	local rigModel = State.activeRigModel or State.lastKnownRigModel
	if not rigModel then
		State.weaponConnectionStatus:set("⚠ No rig selected")
		return
	end

	local container, root = self:resolveWeapon(weapon)
	if not root then
		State.weaponConnectionStatus:set("⚠ Weapon has no parts")
		return
	end

	-- build weapon part set
	local weaponPartSet: { [Instance]: boolean } = {}
	local searchRoot = container or root
	if searchRoot:IsA("BasePart") then
		weaponPartSet[searchRoot] = true
	end
	for _, desc in ipairs(searchRoot:GetDescendants()) do
		if desc:IsA("BasePart") then
			weaponPartSet[desc] = true
		end
	end

	-- search for connection joints
	local searchRoots: { Instance } = { rigModel }
	if container and container ~= rigModel then
		table.insert(searchRoots, container)
	elseif root.Parent and root.Parent ~= rigModel then
		table.insert(searchRoots, root)
	end

	local seen: { [Instance]: boolean } = {}
	for _, sr in ipairs(searchRoots) do
		for _, desc in ipairs(sr:GetDescendants()) do
			if seen[desc] then continue end
			if desc:IsA("Motor6D") or desc:IsA("Weld") or desc:IsA("WeldConstraint") then
				seen[desc] = true
				local j = desc :: any
				local p0, p1 = j.Part0, j.Part1
				if not p0 or not p1 then continue end
				if weaponPartSet[p0] and p1:IsDescendantOf(rigModel) and not weaponPartSet[p1] then
					State.weaponConnectionStatus:set(
						("✓ %s: %s → %s"):format(desc.ClassName, p1.Name, p0.Name)
					)
					return
				elseif weaponPartSet[p1] and p0:IsDescendantOf(rigModel) and not weaponPartSet[p0] then
					State.weaponConnectionStatus:set(
						("✓ %s: %s → %s"):format(desc.ClassName, p0.Name, p1.Name)
					)
					return
				end
			end
		end
	end

	State.weaponConnectionStatus:set("⚠ No connection — equip weapon on character")
end

function ExportManager:exportWeapon()
	-- Export a weapon / accessory with full Motor6D hierarchy.
	-- Mirrors the rig export flow: clone → uniquify → fingerprint → encode → export.

	-- Read from slotted weapon, fall back to current selection
	local weaponInst = State.selectedWeapon:get()
	if not weaponInst then
		local selection = game.Selection:Get()
		if #selection > 0 then
			weaponInst = selection[1]
		else
			warn("[ExportManager] No weapon slotted and nothing selected.")
			return
		end
	end

	local weaponContainer, weaponRoot = self:resolveWeapon(weaponInst)

	if not weaponRoot then
		warn("[ExportManager] Could not find a weapon root part.")
		return
	end

	print(("[ExportManager] Weapon root: %s (container: %s)")
		:format(weaponRoot.Name, weaponContainer and weaponContainer.Name or "<none>"))

	self:clearMetaParts()

	-- ---- Find all connections to the character rig (on the ORIGINAL) ----
	local gripData: { [string]: any } = {}
	local rigModel = State.activeRigModel or State.lastKnownRigModel
	local connectionEntries: { [number]: { joint: Instance, characterPart: BasePart, weaponPart: BasePart } } = {}

	if rigModel then
		-- Build a set of ALL weapon parts so we can detect cross-connections
		local weaponPartSet: { [Instance]: boolean } = {}
		local weaponSearchRoot = weaponContainer or weaponRoot
		if weaponSearchRoot:IsA("BasePart") then
			weaponPartSet[weaponSearchRoot] = true
		end
		for _, desc in ipairs(weaponSearchRoot:GetDescendants()) do
			if desc:IsA("BasePart") then
				weaponPartSet[desc] = true
			end
		end

		local searchRoots: { Instance } = { rigModel }
		if weaponContainer and weaponContainer ~= rigModel then
			table.insert(searchRoots, weaponContainer)
		elseif weaponRoot.Parent and weaponRoot.Parent ~= rigModel then
			table.insert(searchRoots, weaponRoot)
		end

		local seenJointsConn: { [Instance]: boolean } = {}
		local seenWeaponParts: { [Instance]: boolean } = {}
		for _, root in ipairs(searchRoots) do
			for _, desc in ipairs(root:GetDescendants()) do
				if seenJointsConn[desc] then continue end
				if desc:IsA("Motor6D") or desc:IsA("Weld") or desc:IsA("WeldConstraint") then
					seenJointsConn[desc] = true
					local j = desc :: any
					local p0, p1 = j.Part0, j.Part1
					if not p0 or not p1 then continue end
					-- Check if this joint connects a weapon part to a rig part
					if weaponPartSet[p0] and p1:IsDescendantOf(rigModel) and not weaponPartSet[p1] then
						if not seenWeaponParts[p0] then
							seenWeaponParts[p0] = true
							table.insert(connectionEntries, {
								joint = desc,
								characterPart = p1,
								weaponPart = p0,
							})
						end
					elseif weaponPartSet[p1] and p0:IsDescendantOf(rigModel) and not weaponPartSet[p0] then
						if not seenWeaponParts[p1] then
							seenWeaponParts[p1] = true
							table.insert(connectionEntries, {
								joint = desc,
								characterPart = p0,
								weaponPart = p1,
							})
						end
					end
				end
			end
		end
	end

	if #connectionEntries > 0 then
		for _, entry in ipairs(connectionEntries) do
			print(("[ExportManager] Found connection: %s -> %s via %s (%s)")
				:format(entry.characterPart.Name, entry.weaponPart.Name, entry.joint.Name, entry.joint.ClassName))
		end
	else
		if rigModel then
			warn("[ExportManager] No connection joint found between weapon and rig. Weapon will have no positional data.")
		else
			warn("[ExportManager] No active rig. Select a rig first, then select the weapon to export.")
		end
	end

	-- ---- Clone the weapon so we don't modify the original ----
	-- Build an archivable path map before/after clone for original→clone mapping.
	local function buildArchivablePathMap(root: Instance)
		local map: { [string]: Instance } = {}
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

	-- We need a clonable container. If weaponContainer exists, clone it.
	-- Otherwise wrap weaponRoot in a temporary Model to clone.
	local cloneSource: Instance = weaponContainer or weaponRoot
	local originalPathMap = buildArchivablePathMap(cloneSource)

	local wasArchivable = cloneSource.Archivable
	cloneSource.Archivable = true
	local weaponClone = cloneSource:Clone()
	cloneSource.Archivable = wasArchivable

	if not weaponClone then
		warn("[ExportManager] Failed to clone weapon.")
		return
	end

	weaponClone.Parent = game.Workspace
	weaponClone.Archivable = false

	-- Map clone parts → originals
	local clonePathMap = buildArchivablePathMap(weaponClone)
	local originalMap: { [Instance]: Instance } = {}
	local cloneToOriginal: { [Instance]: Instance } = {}
	for path, clonePart in pairs(clonePathMap) do
		local originalPart = originalPathMap[path]
		if originalPart then
			originalMap[clonePart] = originalPart
			cloneToOriginal[clonePart] = originalPart
		end
	end

	-- Find weaponRoot's clone equivalent
	local cloneWeaponRoot: BasePart? = nil
	local originalToClone: { [Instance]: BasePart } = {}

	-- If the clone source IS the weapon root (bare part, no container),
	-- the clone itself is the weapon root — no path map lookup needed.
	if cloneSource == weaponRoot and weaponClone:IsA("BasePart") then
		cloneWeaponRoot = weaponClone :: BasePart
	else
		for clonePart, origPart in pairs(cloneToOriginal) do
			if origPart == weaponRoot and clonePart:IsA("BasePart") then
				cloneWeaponRoot = clonePart :: BasePart
				break
			end
		end
	end
	for clonePart, origPart in pairs(cloneToOriginal) do
		if clonePart:IsA("BasePart") then
			originalToClone[origPart] = clonePart :: BasePart
		end
	end

	if not cloneWeaponRoot then
		warn("[ExportManager] Could not find weapon root in clone.")
		weaponClone:Destroy()
		return
	end

	-- ---- Collect all weapon parts in the clone ----
	-- Build joint cache for the clone
	local weaponJointCache: { [Instance]: { Instance } } = {}
	for _, desc in ipairs(weaponClone:GetDescendants()) do
		if desc:IsA("Motor6D") or desc:IsA("Weld") or desc:IsA("WeldConstraint") then
			local j = desc :: any
			if j.Part0 then
				weaponJointCache[j.Part0] = weaponJointCache[j.Part0] or {}
				table.insert(weaponJointCache[j.Part0], desc)
			end
			if j.Part1 then
				weaponJointCache[j.Part1] = weaponJointCache[j.Part1] or {}
				table.insert(weaponJointCache[j.Part1], desc)
			end
		end
		-- destroy humanoids/animation controllers
		if desc:IsA("Humanoid") or desc:IsA("AnimationController") then
			desc:Destroy()
		end
	end

	local allCloneParts: { BasePart } = {}
	local clonePartSet: { [Instance]: boolean } = {}
	if weaponClone:IsA("BasePart") then
		table.insert(allCloneParts, weaponClone)
		clonePartSet[weaponClone] = true
	end
	for _, desc in ipairs(weaponClone:GetDescendants()) do
		if desc:IsA("BasePart") and not clonePartSet[desc] then
			clonePartSet[desc] = true
			table.insert(allCloneParts, desc)
		end
	end

	-- use container name if it's a Tool/Model/Accessory, else weaponRoot name
	if weaponContainer and (weaponContainer:IsA("Tool") or weaponContainer:IsA("Model") or weaponContainer:IsA("Accoutrement")) then
		gripData.weaponName = weaponContainer.Name
	else
		gripData.weaponName = weaponRoot.Name
	end

	-- ---- Uniquify names, add fingerprints, rename to p<N>x (same as rig export) ----
	local weaponPartEncodeMap: { [Instance]: string } = {}
	local partAuxData = {}
	local usedModelNames: { [string]: boolean } = {}
	local partCount = 0
	local partNames = {}
	local originalNameMap: { [Instance]: string } = {} -- clone inst -> original name

	for _, part in ipairs(allCloneParts) do
		partCount += 1

		-- get original name from the original part
		local origPart = cloneToOriginal[part]
		local origName = if origPart then origPart.Name else part.Name
		originalNameMap[part] = origName

		-- uniquify (clone may have duplicates)
		local baseName = part.Name
		local retryCount = 0
		while usedModelNames[part.Name] do
			retryCount += 1
			part.Name = baseName .. retryCount
		end
		usedModelNames[part.Name] = true

		partNames[partCount] = part.Name
		weaponPartEncodeMap[part] = part.Name

		-- size perturbation fingerprint (same as rig export)
		local quantum = 0.0001
		local perturbation = Vector3.one * (partCount * quantum)
		part.Size = part.Size + perturbation

		table.insert(partAuxData, {
			idx = partCount,
			name = part.Name,
			dims_fp = { part.Size.X, part.Size.Y, part.Size.Z },
			vol_fp = part.Size.X * part.Size.Y * part.Size.Z,
		})

		-- rename to p<N>x (same pattern as rig export for consistent OBJ naming)
		part.Name = ("p%dx"):format(partCount)
	end

	if partCount == 0 then
		warn("[ExportManager] No weapon parts found.")
		weaponClone:Destroy()
		return
	end

	-- ---- Build weapon joint trees from the clone ----
	local visitedParts: { [Instance]: boolean } = {}
	local hasMotor6Ds = false

	local function encodeWeaponPart(part: BasePart, parentPart: BasePart?, connectingJoint: Instance?): any?
		if visitedParts[part] then return nil end
		visitedParts[part] = true

		local elem: any = {
			inst = part,
			jname = originalNameMap[part] or part.Name,
			children = {},
			aux = {},
			auxTransform = {},
			jointType = nil,
		}

		elem.transform = { part.CFrame:GetComponents() }

		if parentPart and connectingJoint then
			local joint = connectingJoint :: any
			if joint:IsA("Motor6D") or joint:IsA("Weld") then
				local parentIsPart0 = (joint.Part0 == parentPart)
				if parentIsPart0 then
					elem.jointtransform0 = { joint.C0:GetComponents() }
					elem.jointtransform1 = { joint.C1:GetComponents() }
				else
					elem.jointtransform0 = { joint.C1:GetComponents() }
					elem.jointtransform1 = { joint.C0:GetComponents() }
				end
				elem.jointType = joint.ClassName
				if joint:IsA("Motor6D") then
					hasMotor6Ds = true
				end
			elseif joint:IsA("WeldConstraint") then
				local parentToChild = parentPart.CFrame:ToObjectSpace(part.CFrame)
				elem.jointtransform0 = { parentToChild:GetComponents() }
				elem.jointtransform1 = { CFrame.new():GetComponents() }
				elem.jointType = "WeldConstraint"
			end
		end

		for _, joint in pairs(weaponJointCache[part] or {}) do
			if (joint :: any).Part0 and (joint :: any).Part1 then
				local subpart: BasePart? = nil
				if (joint :: any).Part0 == part then
					subpart = (joint :: any).Part1
				elseif (joint :: any).Part1 == part then
					subpart = (joint :: any).Part0
				end
				if subpart and subpart ~= parentPart and not visitedParts[subpart] and clonePartSet[subpart] then
					local child = encodeWeaponPart(subpart, part, joint)
					if child then
						table.insert(elem.children, child)
					end
				end
			end
		end

		return elem
	end

	local attachmentByRoot: { [Instance]: { [string]: any } } = {}
	local weaponAttachments: { [number]: { [string]: any } } = {}

	for _, entry in ipairs(connectionEntries) do
		local cloneRoot = originalToClone[entry.weaponPart]
		if cloneRoot and not attachmentByRoot[cloneRoot] then
			local info: { [string]: any } = {
				rootPart = entry.weaponPart.Name,
				suggestedBone = entry.characterPart.Name,
				connectionJointType = entry.joint.ClassName,
			}
			local j = entry.joint :: any
			local charIsPart0 = (j.Part0 == entry.characterPart)
			if j:IsA("Motor6D") or j:IsA("Weld") then
				if charIsPart0 then
					info.connectionC0 = { j.C0:GetComponents() }
					info.connectionC1 = { j.C1:GetComponents() }
				else
					info.connectionC0 = { j.C1:GetComponents() }
					info.connectionC1 = { j.C0:GetComponents() }
				end
			elseif j:IsA("WeldConstraint") then
				local relCF = entry.characterPart.CFrame:ToObjectSpace(entry.weaponPart.CFrame)
				info.connectionC0 = { relCF:GetComponents() }
				info.connectionC1 = { CFrame.new():GetComponents() }
			end
			attachmentByRoot[cloneRoot] = info
			table.insert(weaponAttachments, info)
		end
	end

	-- Build trees for connected components that have rig connections first.
	for cloneRoot, attachment in pairs(attachmentByRoot) do
		local tree = encodeWeaponPart(cloneRoot, nil, nil)
		if tree then
			self:reencodeJointMetadata(tree, weaponPartEncodeMap)
			attachment.joints = tree
		end
	end

	-- Encode any remaining disconnected components (no rig connection).
	for _, part in ipairs(allCloneParts) do
		if not visitedParts[part] then
			local tree = encodeWeaponPart(part, nil, nil)
			if tree then
				self:reencodeJointMetadata(tree, weaponPartEncodeMap)
				table.insert(weaponAttachments, {
					rootPart = originalNameMap[part] or part.Name,
					joints = tree,
				})
			end
		end
	end

	-- Keep backward compatibility with older importers using top-level fields.
	if #weaponAttachments > 0 then
		gripData.weaponAttachments = weaponAttachments
		local first = weaponAttachments[1]
		if first.joints then
			gripData.joints = first.joints
		end
		if first.suggestedBone then
			gripData.suggestedBone = first.suggestedBone
		end
		if first.connectionC0 and first.connectionC1 then
			gripData.connectionC0 = first.connectionC0
			gripData.connectionC1 = first.connectionC1
			gripData.connectionJointType = first.connectionJointType
		end
	end

	-- Build metadata (mirrors generateMetadata output format)
	gripData.parts = partNames
	gripData.partAux = partAuxData
	gripData.partCount = partCount
	gripData.exportType = "weapon"
	gripData.version = "1.2"

	-- ---- Export via ExportSelection (clone + meta parts) ----
	State.metaParts = { weaponClone }

	local metaJson = game.HttpService:JSONEncode(gripData)
	local metaEncoded = BaseXX.to_base32(metaJson):gsub("=", "0")
	local idx = 1
	local segLen = 45
	for begin = 1, #metaEncoded + 1, segLen do
		local metaPart = Instance.new("Part", game.Workspace)
		metaPart.Name = ("meta%dq1%sq1"):format(idx, metaEncoded:sub(begin, begin + segLen - 1))
		State.metaParts[#State.metaParts + 1] = metaPart
		metaPart.Anchored = true
		metaPart.Archivable = false
		idx += 1
	end

	task.wait()

	game.Selection:Set(State.metaParts)
	local attachmentCount = if gripData.weaponAttachments then #gripData.weaponAttachments else 0
	print(("[ExportManager] Exporting weapon '%s' (%d parts, %s Motor6Ds, %d attachment roots)")
		:format(gripData.weaponName, partCount, hasMotor6Ds and "with" or "no", attachmentCount))
	PluginManager():ExportSelection()
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
	
	-- Wait a frame for size changes to propagate before export
	task.wait()
	
	game.Selection:Set(State.metaParts)
	PluginManager():ExportSelection() -- deprecated
end

return ExportManager
