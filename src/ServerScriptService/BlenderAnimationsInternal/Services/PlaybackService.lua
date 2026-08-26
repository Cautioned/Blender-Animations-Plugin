--!strict
-- I moved all the playback logic here to make it easier to manage.
local PlaybackService = {}
PlaybackService.__index = PlaybackService

local RunService = game:GetService("RunService")
local AnimationClipProvider = game:GetService("AnimationClipProvider")
local Utils = require(script.Parent.Parent:WaitForChild("Utils"))
local RigSession = require(script.Parent.Parent.RigSession)

type ConnectionLike = {
	Disconnect: (self: ConnectionLike) -> (),
	Connected: boolean?,
}

type WaitableSignalLike = {
	Connect: (self: WaitableSignalLike, callback: () -> ()) -> ConnectionLike,
	Wait: ((self: WaitableSignalLike) -> ())?,
}

type TrackLike = {
	AdjustSpeed: (self: TrackLike, speed: number) -> (),
	Stop: (self: TrackLike, fadeTime: number?) -> (),
	Destroy: ((self: TrackLike) -> ())?,
	IsPlaying: boolean?,
	Stopped: WaitableSignalLike?,
}

type AnimatorLike = {
	GetPlayingAnimationTracks: (self: AnimatorLike) -> { TrackLike },
	StepAnimations: ((self: AnimatorLike, delta: number) -> ())?,
}

type AnimatorOwnerLike = {
	IsA: (self: AnimatorOwnerLike, className: string) -> boolean,
	FindFirstChildOfClass: ((self: AnimatorOwnerLike, className: string) -> AnimatorLike?)?,
}

type AnimatorInstanceLike = AnimatorOwnerLike & AnimatorLike
type HeartbeatType = { conn: ConnectionLike? }
type KeyframeNameLike = { name: string, time: number, value: string?, type: string? }

local function retimeKeyframeNames(keyframeNames: { KeyframeNameLike }?, speedEnabled: boolean, speedMultiplier: number): { KeyframeNameLike }?
	if not keyframeNames or not speedEnabled or speedMultiplier == 1 then
		return keyframeNames
	end

	local speed = math.clamp(speedMultiplier, 0.05, 10)
	local retimed = table.create(#keyframeNames)
	for index, keyframeName in ipairs(keyframeNames) do
		local copy = table.clone(keyframeName)
		copy.time /= speed
		retimed[index] = copy
	end
	return retimed
end

function PlaybackService.new(State)
	local self = setmetatable({}, PlaybackService)
	self.State = State
	self._playbackToken = 0
	self._delayedReplayToken = 0
	self._delayedReplayPending = false
	self._rigTracks = {} :: { [Instance]: TrackLike }
	return self
end

function PlaybackService:_cancelDelayedReplay()
	self._delayedReplayToken = (self._delayedReplayToken :: number) + 1
	self._delayedReplayPending = false
end

function PlaybackService:_scheduleDelayedReplay(playbackToken: number, callback: () -> ())
	if self._delayedReplayPending then
		return
	end

	self._delayedReplayPending = true
	self._delayedReplayToken = (self._delayedReplayToken :: number) + 1
	local delayedReplayToken = self._delayedReplayToken :: number

	task.delay(1, function()
		if self._delayedReplayToken ~= delayedReplayToken then
			return
		end
		self._delayedReplayPending = false
		if self._playbackToken ~= playbackToken then
			return
		end
		callback()
	end)
end

function PlaybackService:disconnectHeartbeat()
	local heartbeat = self.State.heartbeat :: HeartbeatType
	self:_disconnectConnection(heartbeat.conn)
	heartbeat.conn = nil
end

function PlaybackService:_disconnectConnection(connection: ConnectionLike?)
	if connection and connection.Connected ~= false then
		connection:Disconnect()
	end
end

function PlaybackService:_resetRigPose(rigModel)
	if not rigModel then
		return
	end

	for _, desc in ipairs(rigModel:GetDescendants()) do
		if desc:IsA("Motor6D") then
			desc.Transform = CFrame.identity
		elseif desc:IsA("Bone") then
			desc.Transform = CFrame.identity
		elseif desc:IsA("AnimationConstraint") then
			desc.Transform = CFrame.identity
		end
	end
end

function PlaybackService:_getAnimatorInstance(animatorOwner: AnimatorOwnerLike?): AnimatorInstanceLike?
	if not animatorOwner then
		return nil
	end

	if animatorOwner:IsA("Animator") then
		return animatorOwner :: AnimatorInstanceLike
	end

	local findFirstChildOfClass = animatorOwner.FindFirstChildOfClass
	if (animatorOwner:IsA("Humanoid") or animatorOwner:IsA("AnimationController")) and findFirstChildOfClass then
		local animator = findFirstChildOfClass(animatorOwner, "Animator")
		if animator then
			return animator :: AnimatorInstanceLike
		end
	end

	return nil
end

function PlaybackService:_flushAnimatorPose(animatorOwner: AnimatorOwnerLike?)
	local animator = self:_getAnimatorInstance(animatorOwner)
	if not animator then
		return
	end

	local stepAnimations = animator.StepAnimations
	if not stepAnimations then
		return
	end

	pcall(function()
		stepAnimations(animator, 0)
	end)
end

-- Stop and destroy a single rig's track without affecting other rigs.
-- Used by import/reload flows that should only reset the active rig.
function PlaybackService:stopRigTrack(rigInst: Instance?)
	if not rigInst then
		return
	end

	local track = self._rigTracks[rigInst]
	if track then
		pcall(function() track:AdjustSpeed(0) end)
		pcall(function() track:Stop(0) end)
		pcall(function() track:Destroy() end)
		self._rigTracks[rigInst] = nil
	end

	-- Reset pose for this specific rig
	local snap = self.State.rigSessions[rigInst]
	if snap and snap.activeRigModel then
		self:_resetRigPose(snap.activeRigModel)
		self:_flushAnimatorPose(snap.activeAnimator)
	end
end

function PlaybackService:stopAnimationAndDisconnect(_options: any?)
	self:_cancelDelayedReplay()

	self._playbackToken = (self._playbackToken :: number) + 1
	local heartbeatToDisconnect = self.State.heartbeat.conn

	-- Stop all per-rig tracks
	for rigInst, track in pairs(self._rigTracks) do
		pcall(function() track:AdjustSpeed(0) end)
		pcall(function() track:Stop(0) end)
	end

	-- Also stop the legacy single track if any
	local currentTrack = self.State.currentAnimTrack :: TrackLike?
	if currentTrack then
		pcall(function() currentTrack:AdjustSpeed(0) end)
		pcall(function() currentTrack:Stop(0) end)
	end

	-- Destroy all tracks BEFORE clearing the table
	for rigInst, track in pairs(self._rigTracks) do
		pcall(function() track:Destroy() end)
	end
	if currentTrack then
		pcall(function() currentTrack:Destroy() end)
	end

	-- Clear all track state
	self._rigTracks = {}
	self.State.currentAnimTrack = nil
	self.State.heartbeat.conn = nil
	self.State.isPlaying:set(false)
	self.State.isFinished:set(false)

	self:_disconnectConnection(heartbeatToDisconnect)

	-- Reset pose on all known rigs
	for rigInst, _ in pairs(self.State.rigSessions) do
		local snap = self.State.rigSessions[rigInst]
		if snap and snap.activeRigModel then
			self:_resetRigPose(snap.activeRigModel)
			self:_flushAnimatorPose(snap.activeAnimator)
		end
	end
end

function PlaybackService:updateUI()
	local isPlaying = self.State.isPlaying:get()
	local isReversed = self.State.isReversed:get()
	
	if isPlaying then
		if isReversed then
			-- Playing in reverse: show pause on reverse button, play on main button
			self.State.playPauseButtonImage:set("rbxasset://textures/AnimationEditor/button_control_play.png")
			self.State.reversePlayPauseButtonImage:set("rbxasset://textures/AnimationEditor/button_pause_white@2x.png")
		else
			-- Playing forward: show pause on main button, reverse on reverse button
			self.State.playPauseButtonImage:set("rbxasset://textures/AnimationEditor/button_pause_white@2x.png")
			self.State.reversePlayPauseButtonImage:set("rbxasset://textures/AnimationEditor/button_control_reverseplay.png")
		end
	else
		-- Not playing: show play on main button, reverse on reverse button
		self.State.playPauseButtonImage:set("rbxasset://textures/AnimationEditor/button_control_play.png")
		self.State.reversePlayPauseButtonImage:set("rbxasset://textures/AnimationEditor/button_control_reverseplay.png")
	end
end

function PlaybackService:seekAnimationToTime(timePosition: number)
	local anyTrack = false
	-- Seek all per-rig tracks
	for _, track in pairs(self._rigTracks) do
		anyTrack = true
		local clampedTimePosition = math.clamp(timePosition, 0, track.Length - 0.001)
		track.TimePosition = clampedTimePosition
	end
	-- Legacy single-track fallback
	if not anyTrack and self.State.currentAnimTrack then
		local animTrack = self.State.currentAnimTrack :: AnimationTrack
		if self.State.animationLength:get() then
			local clampedTimePosition = math.clamp(timePosition, 0, animTrack.Length - 0.001)
			animTrack.TimePosition = clampedTimePosition
			anyTrack = true
		end
	end
	if not anyTrack then
		warn("There's nothing to seek, import animation data.")
	end
end

function PlaybackService:onPlayPauseButtonActivated()
	if self.State.isPlaying:get() then
		-- Currently playing → pause all
		self.State.isPlaying:set(false)
		for _, track in pairs(self._rigTracks) do
			pcall(function() track:AdjustSpeed(0) end)
		end
		if self.State.currentAnimTrack then
			pcall(function() (self.State.currentAnimTrack :: TrackLike):AdjustSpeed(0) end)
		end
	else
		-- Not playing → start or resume
		local hasTracks = next(self._rigTracks) ~= nil
			or self.State.currentAnimTrack ~= nil

		if not hasTracks then
			-- First play: build tracks for all rigs with animation data
			self:playAllRigs()
			return -- playAllRigs already sets isPlaying and starts playback
		end

		-- Resume existing tracks
		self.State.isPlaying:set(true)
		self.State.isReversed:set(false)
		if self.State.isFinished:get() then
			self.State.isFinished:set(false)
			self:seekAnimationToTime(0)
		end
		for _, track in pairs(self._rigTracks) do
			pcall(function() track:AdjustSpeed(1) end)
		end
		if self.State.currentAnimTrack then
			pcall(function() (self.State.currentAnimTrack :: TrackLike):AdjustSpeed(1) end)
		end
	end
	self:updateUI()
end

function PlaybackService:onReverseButtonActivated()
	if self.State.isPlaying:get() and self.State.isReversed:get() then
		-- Currently playing in reverse → stop
		self.State.isPlaying:set(false)
		for _, track in pairs(self._rigTracks) do
			pcall(function() track:AdjustSpeed(0) end)
		end
		if self.State.currentAnimTrack then
			pcall(function() (self.State.currentAnimTrack :: TrackLike):AdjustSpeed(0) end)
		end
	else
		local hasTracks = next(self._rigTracks) ~= nil
			or self.State.currentAnimTrack ~= nil

		if not hasTracks then
			-- First play (reverse): build tracks, then reverse
			self:playAllRigs()
			-- After playAllRigs, tracks are playing forward; flip to reverse
			self.State.isReversed:set(true)
			if self.State.playhead:get() == 0 and self.State.animationLength:get() then
				self:seekAnimationToTime(self.State.animationLength:get())
			end
			for _, track in pairs(self._rigTracks) do
				pcall(function() track:AdjustSpeed(-1) end)
			end
			if self.State.currentAnimTrack then
				pcall(function() (self.State.currentAnimTrack :: TrackLike):AdjustSpeed(-1) end)
			end
			return
		end

		-- Resume existing tracks in reverse
		self.State.isPlaying:set(true)
		self.State.isReversed:set(true)
		if self.State.playhead:get() == 0 and self.State.animationLength:get() then
			self:seekAnimationToTime(self.State.animationLength:get())
		end
		for _, track in pairs(self._rigTracks) do
			pcall(function() track:AdjustSpeed(-1) end)
		end
		if self.State.currentAnimTrack then
			pcall(function() (self.State.currentAnimTrack :: TrackLike):AdjustSpeed(-1) end)
		end
	end
	self:updateUI()
end

function PlaybackService:onSliderChange(newValue: number)
	local hasTracks = next(self._rigTracks) ~= nil or self.State.currentAnimTrack ~= nil
	if not hasTracks then return end

	local wasPlaying = self.State.isPlaying:get()
	local wasReversed = self.State.isReversed:get()

	-- Pause all while seeking
	for _, track in pairs(self._rigTracks) do
		pcall(function() track:AdjustSpeed(0) end)
	end
	if self.State.currentAnimTrack then
		pcall(function() (self.State.currentAnimTrack :: TrackLike):AdjustSpeed(0) end)
	end
	self:seekAnimationToTime(newValue)

	-- Resume if was playing
	if wasPlaying then
		for _, track in pairs(self._rigTracks) do
			pcall(function() track:AdjustSpeed(wasReversed and -1 or 1) end)
		end
		if self.State.currentAnimTrack then
			pcall(function() (self.State.currentAnimTrack :: TrackLike):AdjustSpeed(wasReversed and -1 or 1) end)
		end
	end
end

function PlaybackService:playCurrentAnimation(activeAnimator, kfsOverride)
	self:stopAnimationAndDisconnect()
	self:updateUI()

	if not activeAnimator then
		warn("Animator not found")
		return
	end

	local animator = activeAnimator:FindFirstChildOfClass("Animator")
	if not animator and self.State.activeRigModel then
		local newAnimator = Instance.new("Animator")
		local parent = self.State.activeRigModel:FindFirstChildWhichIsA("Humanoid")
			or self.State.activeRigModel:FindFirstChildWhichIsA("AnimationController")
		if parent then
			newAnimator.Parent = parent
			animator = newAnimator
		end
	end

	if not animator then
		warn("Failed to find or create animator")
		return
	end

	if not self.State.activeRig then
		warn("No active rig to create animation from")
		return
	end

	-- Sync keyframe names/markers before creating animation (when not using override)
	if not kfsOverride then
		self.State.activeRig.keyframeNames = retimeKeyframeNames(
			self.State.keyframeNames:get() :: { KeyframeNameLike }?,
			self.State.speedEnabled:get(),
			self.State.speedMultiplier:get()
		)
	end

	local kfs = kfsOverride or self.State.activeRig:ToRobloxAnimation()
	-- only scale if we're creating a new animation from the rig (no kfsOverride)
	-- if kfsOverride is provided, it's already been scaled by the caller
	if not kfsOverride and self.State.scaleFactor:get() ~= 1 then
		kfs = Utils.scaleAnimation(kfs, self.State.scaleFactor:get())
	end
	self.State.currentKeyframeSequence = kfs

	self.State.animationLength:set(Utils.getRealKeyframeDuration(kfs:GetKeyframes()))
	local animID = AnimationClipProvider:RegisterAnimationClip(kfs)

	local animation = Instance.new("Animation")
	animation.AnimationId = animID

	if animator then
		self.State.currentAnimTrack = animator:LoadAnimation(animation)
	end

    if self.State.currentAnimTrack then
        local animTrack = self.State.currentAnimTrack :: AnimationTrack
        animTrack.Looped = false
        -- explicitly set forward play state instead of toggling
        self.State.isReversed:set(false)
        self.State.isFinished:set(false)
        self.State.isPlaying:set(true)
        animTrack:AdjustSpeed(1)
        self:updateUI()
    else
		self:stopAnimationAndDisconnect()
		warn("Failed to load animation track.")
	end

	local function playAnimation()
        if self.State.currentAnimTrack then
            local animTrack = self.State.currentAnimTrack :: AnimationTrack
			self:_cancelDelayedReplay()
            animTrack.TimePosition = 0
            animTrack:Play()
			animTrack:AdjustSpeed(1)
            -- ensure ui reflects the current state
            self.State.isPlaying:set(true)
            self.State.isReversed:set(false)
			self.State.isFinished:set(false)
            self:updateUI()
        end
    end

	playAnimation()
	local playbackToken = self._playbackToken :: number

	local lastStepTime = tick()

	self:disconnectHeartbeat()
	self.State.heartbeat.conn = RunService.Heartbeat:Connect(function(step)
		if self._playbackToken ~= playbackToken then
			return
		end

		local currentTime = tick()
		local delta = currentTime - lastStepTime
		lastStepTime = currentTime

		if not self.State.userChangingSlider:get() and self.State.currentAnimTrack then
			local animTrack = self.State.currentAnimTrack :: AnimationTrack
			if animTrack.TimePosition then
				self.State.playhead:set(animTrack.TimePosition)
			end
		end

		local animLength = self.State.animationLength:get()
		if animLength and animLength > 0 then
			if self.State.currentAnimTrack then
				local animTrack = self.State.currentAnimTrack :: AnimationTrack
				if animTrack.TimePosition >= animLength - 0.01 then
					if self.State.loopAnimation:get() and self.State.isPlaying:get() then
						playAnimation()
					else
						if self.State.isPlaying:get() then
							animTrack:AdjustSpeed(0)
							self.State.isPlaying:set(false)
							self.State.isFinished:set(true)
							self:updateUI()
							self:_scheduleDelayedReplay(playbackToken, function()
								if self.State.currentAnimTrack ~= animTrack then
									return
								end
								playAnimation()
							end)
						end
					end
				elseif animTrack.TimePosition <= 0 then
					if self.State.isReversed:get() and self.State.loopAnimation:get() and self.State.isPlaying:get() then
						if self.State.animationLength:get() then
							self:seekAnimationToTime(self.State.animationLength:get())
						end
					elseif self.State.isReversed:get() and self.State.isPlaying:get() then
						if self.State.isPlaying:get() then
							self.State.isPlaying:set(false)
							self:updateUI()
						end
					end
				end
			end
		else
			warn("No Animation Data.")
			self.State.isPlaying:set(false)
			self:disconnectHeartbeat()
		end

		if animator then
			animator:StepAnimations(delta)
		end
	end)
end

-- Multi-rig global playback: load and play animation for every rig that has
-- animationData loaded in its session.  All rigs play in sync, driven by a
-- single heartbeat.  The global playhead tracks the first active track.
function PlaybackService:playAllRigs()
	self:stopAnimationAndDisconnect()
	self:updateUI()

	-- Always persist the currently-viewed rig's state so it's included.
	local activeRigInst = self.State.activeSessionRig:get()
	if activeRigInst then
		if self.State.rigSessionManager then
			self.State.rigSessionManager:saveActive()
		else
			self.State.rigSessions[activeRigInst] = RigSession.createSnapshot(self.State)
		end
	end

	local sessions = self.State.rigSessions

	if not sessions or next(sessions) == nil then
		-- No sessions at all — fall back to single-rig playback
		if self.State.activeRig and self.State.activeAnimator and self.State.animationData then
			warn("[playAllRigs] no sessions, falling back to single-rig")
			self:playCurrentAnimation(self.State.activeAnimator)
			return
		end
		warn("No rig sessions available and no active rig to fall back to.")
		return
	end

	-- Snapshot the "viewed" rig so we can restore it after generating KFS for all rigs
	local viewedRig = self.State.activeSessionRig:get()
	local savedActiveRig = self.State.activeRig
	local savedActiveAnimator = self.State.activeAnimator
	local savedActiveRigModel = self.State.activeRigModel
	local savedAnimationData = self.State.animationData
	local savedKeyframeNames = self.State.keyframeNames:get()
	local savedCurrentKFS = self.State.currentKeyframeSequence
	local savedScaleFactor = self.State.scaleFactor:get()

	local maxLength = 0
	local anyLoaded = false

	for rigInst, snap in pairs(sessions) do
		if not snap.activeRig or not snap.activeAnimator or not snap.animationData then
			continue
		end

		-- Temporarily swap to this rig's state for ToRobloxAnimation
		self.State.activeRig = snap.activeRig
		self.State.activeAnimator = snap.activeAnimator
		self.State.activeRigModel = snap.activeRigModel
		self.State.animationData = snap.animationData
		self.State.keyframeNames:set(snap.keyframeNames or {})
		self.State.scaleFactor:set(snap.scaleFactor or 1)

		-- Sync keyframe names into the rig
		snap.activeRig.keyframeNames = retimeKeyframeNames(
			snap.keyframeNames or {},
			snap.speedEnabled == true,
			snap.speedMultiplier or 1
		) or {}

		local ok, kfs = pcall(function()
			return snap.activeRig:ToRobloxAnimation()
		end)
		if not ok or not kfs then
			warn("Failed to generate KFS for rig:", snap.rigModelName or rigInst.Name)
			continue
		end

		if snap.scaleFactor and snap.scaleFactor ~= 1 then
			kfs = Utils.scaleAnimation(kfs, snap.scaleFactor)
		end
		self.State.currentKeyframeSequence = kfs

		local duration = Utils.getRealKeyframeDuration(kfs:GetKeyframes())
		if duration > maxLength then
			maxLength = duration
		end

		local animID = AnimationClipProvider:RegisterAnimationClip(kfs)
		local animation = Instance.new("Animation")
		animation.AnimationId = animID

		local animator = snap.activeAnimator:FindFirstChildOfClass("Animator")
		if not animator and snap.activeRigModel then
			local newAnimator = Instance.new("Animator")
			local parent = snap.activeRigModel:FindFirstChildWhichIsA("Humanoid")
				or snap.activeRigModel:FindFirstChildWhichIsA("AnimationController")
			if parent then
				newAnimator.Parent = parent
				animator = newAnimator
			end
		end

		if animator then
			local track = animator:LoadAnimation(animation)
			if track then
				track.Looped = false
				self._rigTracks[rigInst] = track
				anyLoaded = true
			end
		end
	end

	-- Restore the viewed rig's state
	self.State.activeRig = savedActiveRig
	self.State.activeAnimator = savedActiveAnimator
	self.State.activeRigModel = savedActiveRigModel
	self.State.animationData = savedAnimationData
	self.State.keyframeNames:set(savedKeyframeNames)
	self.State.currentKeyframeSequence = savedCurrentKFS
	self.State.scaleFactor:set(savedScaleFactor)

	if not anyLoaded then
		warn("No rigs have animation data to play.")
		return
	end

	self.State.animationLength:set(maxLength)

	-- Set legacy track to first rig track for backward compat
	local firstTrack: TrackLike?
	for _, track in pairs(self._rigTracks) do
		firstTrack = track
		break
	end
	self.State.currentAnimTrack = firstTrack

	-- Start all tracks playing forward
	self.State.isReversed:set(false)
	self.State.isFinished:set(false)
	self.State.isPlaying:set(true)
	for _, track in pairs(self._rigTracks) do
		track:AdjustSpeed(1)
	end
	self:updateUI()

	local function replayAll()
		self:_cancelDelayedReplay()
		for _, track in pairs(self._rigTracks) do
			pcall(function()
				track.TimePosition = 0
				track:Play()
				track:AdjustSpeed(1)
			end)
		end
		self.State.isPlaying:set(true)
		self.State.isReversed:set(false)
		self.State.isFinished:set(false)
		self:updateUI()
	end

	replayAll()
	local playbackToken = self._playbackToken :: number
	local lastStepTime = tick()

	self:disconnectHeartbeat()
	self.State.heartbeat.conn = RunService.Heartbeat:Connect(function(step)
		if self._playbackToken ~= playbackToken then
			return
		end

		local currentTime = tick()
		local delta = currentTime - lastStepTime
		lastStepTime = currentTime

		-- Update global playhead from the first active track
		if not self.State.userChangingSlider:get() then
			for _, track in pairs(self._rigTracks) do
				if track.TimePosition then
					self.State.playhead:set(track.TimePosition)
					break
				end
			end
		end

		local hasTracks = next(self._rigTracks) ~= nil
		if hasTracks then
			-- Check if ALL tracks have finished (each against its own Length)
			local allFinished = true
			local anyReversedBoundary = false
			for _, track in pairs(self._rigTracks) do
				if track.TimePosition then
					local trackLen = track.Length
					if trackLen and trackLen > 0 then
						-- Forward finish: TimePosition at or past this track's own end
						if track.TimePosition < trackLen - 0.01 then
							allFinished = false
						end
						-- Reverse boundary: TimePosition at or below 0
						if track.TimePosition <= 0 then
							anyReversedBoundary = true
						end
					end
				end
			end

			if allFinished then
				if self.State.loopAnimation:get() and self.State.isPlaying:get() then
					replayAll()
				else
					if self.State.isPlaying:get() then
						for _, track in pairs(self._rigTracks) do
							pcall(function() track:AdjustSpeed(0) end)
						end
						self.State.isPlaying:set(false)
						self.State.isFinished:set(true)
						self:updateUI()
						self:_scheduleDelayedReplay(playbackToken, function()
							replayAll()
						end)
					end
				end
			end

			-- Check reverse boundary
			if anyReversedBoundary then
				if self.State.isReversed:get() and self.State.loopAnimation:get() and self.State.isPlaying:get() then
					self:seekAnimationToTime(self.State.animationLength:get())
				elseif self.State.isReversed:get() and self.State.isPlaying:get() then
					self.State.isPlaying:set(false)
					self:updateUI()
				end
			end
		else
			self.State.isPlaying:set(false)
			self:disconnectHeartbeat()
		end

		-- Step animations on all rigs
		for rigInst, _ in pairs(self._rigTracks) do
			local snap = sessions[rigInst]
			if snap and snap.activeAnimator then
				local anim = snap.activeAnimator:FindFirstChildOfClass("Animator")
				if anim and anim.StepAnimations then
					pcall(function() anim:StepAnimations(delta) end)
				end
			end
		end
	end)
end

return PlaybackService
