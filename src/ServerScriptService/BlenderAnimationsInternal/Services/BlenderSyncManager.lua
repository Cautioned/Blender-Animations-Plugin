--!native
--!strict
--!optimize 2

local State = require(script.Parent.Parent.state)
local _Types = require(script.Parent.Parent.types)
local _PlaybackService = require(script.Parent.PlaybackService)

local BlenderConnection = require(script.Parent.Parent.Components.BlenderConnection)

local BlenderSyncManager = {}
BlenderSyncManager.__index = BlenderSyncManager

function BlenderSyncManager.new(playbackService: any, animationManager: any)
	local self = setmetatable({}, BlenderSyncManager)
	
	self.playbackService = playbackService
	self.animationManager = animationManager
	self.blenderConnectionService = BlenderConnection.new(game:GetService("HttpService")) :: any
	self.liveSyncCoroutine = nil :: thread?
	self.periodicRefreshCoroutine = nil :: thread?
	
	return self
end

function BlenderSyncManager:updateAvailableArmatures()
	local armatures = self.blenderConnectionService:ListArmatures(State.serverPort:get())

	if armatures then
		State.availableArmatures:set(armatures)
		State.serverStatus:set("Connected")
		print("Auto-refreshed armatures:", #armatures, "found")
		
		-- Auto-select if there's only one armature and none is currently selected
		if #armatures == 1 and not State.selectedArmature:get() then
			State.selectedArmature:set(armatures[1])
			print("Auto-selected single armature:", armatures[1].name)
			
			-- Auto-start live sync if enabled and there's only one armature
			if State.liveSyncEnabled:get() and State.isServerConnected:get() then
				print("Auto-starting live sync for single armature:", armatures[1].name)
				self:startLiveSyncing()
			end
		end
		
		return true
	else
		State.availableArmatures:set({})
		State.serverStatus:set("Disconnected")
		return false
	end
end

function BlenderSyncManager:importAnimationFromBlender()
	if not State.selectedArmature:get() then
		warn("No armature selected")
		return false
	end

	local armature = State.selectedArmature:get()
	if not armature then
		warn("No armature selected")
		return false
	end

	local responseBody = self.blenderConnectionService:ImportAnimation(State.serverPort:get(), (armature :: any).name)

	if responseBody then
		-- The response is binary, so we pass `true`
		local success = self.animationManager:loadAnimDataFromText(responseBody, true)
		if success then
			-- Don't set the hash here, it will be set in the polling loop
		end
		return success
	else
		warn("Failed to import animation from blender.")
		return false
	end
end

function BlenderSyncManager:exportAnimationToBlender()
	if not State.isServerConnected:get() then
		warn("Not connected to Blender server.")
		return false
	end

	if not State.currentKeyframeSequence then
		warn("No active animation to export.")
		return false
	end

	if not State.activeRig then
		warn("No active rig found to serialize animation from.")
		return false
	end

	local AnimationSerializer = require(script.Parent.Parent.Components.AnimationSerializer)
	local animationSerializerService = AnimationSerializer.new()
	
	local animData = animationSerializerService:serialize(State.currentKeyframeSequence, State.activeRig)
	if not animData then
		warn("Failed to serialize animation.")
		return false
	end

	-- Get target armature from selected armature
	local targetArmature = nil
	if State.selectedArmature:get() then
		targetArmature = (State.selectedArmature:get() :: any).name
	end
	
	return self.blenderConnectionService:ExportAnimation(State.serverPort:get(), animData, targetArmature)
end

function BlenderSyncManager:stopLiveSyncing()
	if self.liveSyncCoroutine then
		coroutine.close(self.liveSyncCoroutine :: thread)
		self.liveSyncCoroutine = nil
		print("Live sync stopped.")
	end
end

function BlenderSyncManager:startLiveSyncing()
	self:stopLiveSyncing() -- Stop any existing sync loops

	if not State.liveSyncEnabled:get() then
		return
	end

	self.liveSyncCoroutine = coroutine.create(function()
		-- print("Live sync started.")
		local pollInterval = 0.033  -- Start with fast polling
		local noChangeCount = 0
		local maxPollInterval = 2.0  -- Maximum 2 seconds between polls
		local lastArmatureRefresh = 0
		local armatureRefreshInterval = 5.0  -- Refresh armatures every 5 seconds
		
		while State.liveSyncEnabled:get() do
			-- Skip polling if widget is not enabled to reduce performance impact
			if not State.widgetsEnabled:get() then
				task.wait(1) -- Wait longer when widget is hidden
				continue
			end
			local isConnected = State.isServerConnected:get()
			local selectedArmature = State.selectedArmature:get()

			if isConnected then
				-- Periodic armature refresh
				local currentTime = tick()
				if currentTime - lastArmatureRefresh > armatureRefreshInterval then
					print("Auto-refreshing armatures...")
					self:updateAvailableArmatures()
					lastArmatureRefresh = currentTime
				end
				
				if selectedArmature then
					local armatureName = (selectedArmature :: any).name
					local lastHash = State.lastKnownBlenderAnimHash:get()
					local serverPort = State.serverPort:get()

					local status, err = pcall(
						self.blenderConnectionService.CheckAnimationStatus,
						self.blenderConnectionService,
						serverPort,
						armatureName,
						lastHash
					)

					if not status then
						if State.serverStatus:get() ~= "Live Sync: Connection lost" then
							State.serverStatus:set("Live Sync: Connection lost")
						end
						-- Reset polling on connection loss
						pollInterval = 0.033
						noChangeCount = 0
					else
						if State.serverStatus:get() == "Live Sync: Connection lost" then
							State.serverStatus:set("Connected") -- Restore status
						end
						
						if (err :: any) and (err :: any).has_update then
							self:importAnimationFromBlender()
							State.lastKnownBlenderAnimHash:set((err :: any).hash)
							-- Reset to fast polling when changes detected
							pollInterval = 0.033
							noChangeCount = 0
						else
							-- No changes detected, gradually increase polling interval
							noChangeCount += 1
							pollInterval = math.min(0.033 + (noChangeCount * 0.033), maxPollInterval)
						end
					end
				end
			end
			
			task.wait(pollInterval)
		end
		-- print("Live sync coroutine finished.")
	end)

	if self.liveSyncCoroutine then
		task.spawn(self.liveSyncCoroutine)
	end
end


function BlenderSyncManager:cleanupServerConnection()
	State.isServerConnected:set(false)
	State.serverStatus:set("Disconnected")
	self:stopLiveSyncing() -- Stop live sync when disconnecting
	
	-- Any other network cleanup can go here
end

function BlenderSyncManager:toggleServerConnection()
	if not State.isServerConnected:get() then
		print("Attempting to connect to Blender server...")
		local success = self:updateAvailableArmatures()
		State.isServerConnected:set(success)
		if not success then
			warn("Failed to establish connection")
			self:cleanupServerConnection()
		else
			print("Successfully connected to Blender server")
		end
	else
		self:cleanupServerConnection()
	end
end

function BlenderSyncManager:fetchAnimationFromServer()
	-- ALL LOGIC MOVED TO BlenderConnection.lua
	return false
end

function BlenderSyncManager:cleanup()
	self:stopLiveSyncing()
	self:cleanupServerConnection()
end

return BlenderSyncManager



