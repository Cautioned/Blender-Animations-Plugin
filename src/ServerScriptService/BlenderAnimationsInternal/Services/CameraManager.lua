--!native
--!strict
--!optimize 2

local State = require(script.Parent.Parent.state)
local RunService = game:GetService("RunService")
local Fusion = require(script.Parent.Parent.Packages.Fusion)

local Value = Fusion.Value

local CameraManager = {}
CameraManager.__index = CameraManager

function CameraManager.new()
	local self = setmetatable({}, CameraManager)
	
	-- Create a reactive state for the rig parts list
	self.rigPartsList = Value({})
	
	return self
end

function CameraManager:attachCameraToPart(part: BasePart?)
	if not part then
		return
	end

	if State.cameraConnection then
		State.cameraConnection:Disconnect()
		State.cameraConnection = nil
	end

	local camera = game.Workspace.CurrentCamera
	State.isCameraAttached:set(true)
	camera.CameraType = Enum.CameraType.Scriptable
	camera.CameraSubject = part
	camera.FieldOfView = State.fovValue:get()
	camera.CFrame = CFrame.new(part.Position, part.Position + part.CFrame.LookVector)

	State.cameraConnection = RunService.RenderStepped:Connect(function()
		if State.isCameraAttached:get() then
			camera.CFrame = part.CFrame
		end
	end)
end

function CameraManager:detachCamera()
	if State.cameraConnection then
		State.cameraConnection:Disconnect()
		State.cameraConnection = nil
	end

	local camera = game.Workspace.CurrentCamera
	if State.isCameraAttached:get() then
		State.isCameraAttached:set(false)
		camera.CameraType = Enum.CameraType.Fixed
		camera.CameraSubject = nil
		camera.FieldOfView = 70
		State.fovValue:set(70)
	end
end

function CameraManager:updatePartsList()
	if not State.activeRig then
		self.rigPartsList:set({})
		return
	end

	local newParts = {}
	for _, rigPart in pairs(State.activeRig.bones) do
		-- Include all parts (Part, MeshPart, UnionOperation, etc.) - anything that's an Instance
		if rigPart.part and typeof(rigPart.part) == "Instance" then
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

	self.rigPartsList:set(newParts)
end

function CameraManager:getPartsList()
	return self.rigPartsList
end

function CameraManager:cleanup()
	self:detachCamera()
end

return CameraManager
