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

function AnimationManager.new(playbackService: any, pluginObj: Plugin?)
	local self = setmetatable({}, AnimationManager)
	
	self.playbackService = playbackService
	self.plugin = pluginObj
	self.animationSerializerService = AnimationSerializer.new()
	
	return self
end

function AnimationManager:displayDetailedError(title, message)
	warn(title, message)
end

function AnimationManager:loadAnim(data: string, isBinary: boolean)
	local animData = self.animationSerializerService:deserialize(data, isBinary)

	if not animData then
		error("Failed to deserialize animation data.")
	end

	-- Load the animation
	local _loadSuccess, loadError = pcall(function()
		assert(State.activeRig, "activeRig is nil")
		State.activeRig:LoadAnimation(animData)
		return true
	end)

	if not _loadSuccess then
		error("Animation loading failed: " .. tostring(loadError))
	end
end

function AnimationManager:loadAnimDataFromText(text: string, isBinary: boolean)
	local ok = pcall(self.loadAnim, self, text, isBinary)
	if ok then
		local success, result = pcall(self.loadRig, self)
		if success then
			print("Animation loaded successfully.")
			return true
		else
			self:displayDetailedError("Error during rig loading", tostring(result))
			return false
		end
	else
		self:displayDetailedError("Error during animation data loading", "Unknown error")
		return false
	end
end

-- Legacy-friendly loader: try binary first, then base64 text fallback.
function AnimationManager:loadAnimDataAuto(text: string)
    if self:loadAnimDataFromText(text, true) then
        return true
    end
    return self:loadAnimDataFromText(text, false)
end

function AnimationManager:loadRig(animationToLoad: KeyframeSequence?)
	self.playbackService:stopAnimationAndDisconnect()

	if not State.activeRig then
		error("No active rig available")
	end

	local kfs: KeyframeSequence
	if animationToLoad then
		kfs = animationToLoad:Clone()
	else
		kfs = State.activeRig:ToRobloxAnimation()
	end

	if State.scaleFactor:get() ~= 1 then
		kfs = Utils.scaleAnimation(kfs, State.scaleFactor:get()) -- Scale the animation
	end

	State.animationData = (kfs:GetKeyframes() :: any) :: { Types.KeyframeType }?
	State.animationLength:set(Utils.getAnimDuration(State.animationData))
	self.playbackService:playCurrentAnimation(State.activeAnimator, kfs)

	-- Calculate keyframe statistics
	local keyframes = (kfs:GetKeyframes() :: any) :: { Types.KeyframeType }?
	local count = keyframes and #keyframes or 0
	local totalDuration = keyframes and Utils.getAnimDuration(keyframes) or 0

	State.keyframeStats:set({
		count = count,
		totalDuration = totalDuration,
	})
	return true
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

	local kfs = self:createKeyframeSequenceFromState()
	if not kfs then
		return
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
	assert(State.activeRig)
	local kfs = State.activeRig:ToRobloxAnimation()
	if State.scaleFactor:get() ~= 1 then
		kfs = Utils.scaleAnimation(kfs, State.scaleFactor:get()) -- Scale the animation
	end

	if name then
		kfs.Name = name
	end

	kfs.Parent = folder

	kfs.Priority = priorityLookup[State.selectedPriority:get()]
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

	local kfs = self:createKeyframeSequenceFromState()
	if not kfs then
		return
	end
	kfs.Parent = game.Workspace

	
	SelectionService:Set({kfs})
	self.plugin:SaveSelectedToRoblox()

	task.wait(2)
	kfs:Destroy()
end

function AnimationManager:playSavedAnimation(animation)
	if not animation or not (animation :: any).instance then
		return
	end

	self:loadRig((animation :: any).instance)
end

function AnimationManager:importAnimationsBulk()
	if State.activeRig then
		self.playbackService:stopAnimationAndDisconnect({ background = true })

		local animfiles = game:GetService("StudioService"):PromptImportFiles({ "rbxanim" })

		if animfiles then
			if #animfiles > 1 then
                for _, animfile in ipairs(animfiles) do
					self.playbackService:stopAnimationAndDisconnect({ background = true })

					local loaded = (animfile :: any):GetBinaryContents()
                    local success = pcall(function()
                        self:loadAnimDataAuto(loaded)
                    end)
					if success then
						local name = string.gsub(animfile.Name, ".rbxanim", "")
						self:saveAnimationFolder(name)
					else
						warn("Error loading animation")
					end
				end
			else
                for _, animfile in ipairs(animfiles) do
					self.playbackService:stopAnimationAndDisconnect({ background = true })

					local loaded = (animfile :: any):GetBinaryContents()
                    pcall(function()
                        self:loadAnimDataAuto(loaded)
                    end)
				end
			end
		else
			-- Handle the case where no files were selected or the operation was canceled.
			print("No files were imported.")
		end
	else
		warn("No active rig set for bulk importing animations.")
	end
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

return AnimationManager



