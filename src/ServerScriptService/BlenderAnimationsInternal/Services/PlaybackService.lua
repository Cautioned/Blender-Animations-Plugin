--!strict
-- I moved all the playback logic here to make it easier to manage.
local PlaybackService = {}
PlaybackService.__index = PlaybackService

local RunService = game:GetService("RunService")
local AnimationClipProvider = game:GetService("AnimationClipProvider")
local Utils = require(script.Parent.Parent:WaitForChild("Utils"))

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
type TrackSet = { [TrackLike]: boolean }
type HeartbeatType = { conn: ConnectionLike? }
type StopOptions = { background: boolean?, animatorOverride: AnimatorOwnerLike? }
type KeyframeNameLike = { name: string, time: number, value: string?, type: string? }

function PlaybackService.new(State, Types)
	local self = setmetatable({}, PlaybackService)
	self.State = State
	self.Types = Types
	self._playbackToken = 0
	self._delayedReplayToken = 0
	self._delayedReplayPending = false
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

function PlaybackService:_cleanupAnimation(
	animatorToStop: AnimatorOwnerLike?,
	heartbeatToDisconnect: ConnectionLike?,
	rigModel,
	alreadyStoppedTracks: TrackSet?
)
	local success, err = pcall(function()
		local animator = self:_getAnimatorInstance(animatorToStop)
		if animator then
			local tracks = animator:GetPlayingAnimationTracks()
				if #tracks > 0 then
					for _, track in ipairs(tracks) do
						if alreadyStoppedTracks and alreadyStoppedTracks[track] then
							continue
						end
						local stoppedSignal = track.Stopped
						if stoppedSignal then
							local stopped = false
							local stopConn = stoppedSignal:Connect(function()
								stopped = true
							end)
							track:Stop(0.05)
							local waitForStopped = stoppedSignal.Wait
							if track.IsPlaying and waitForStopped then
								waitForStopped(stoppedSignal)
							elseif not stopped then
								task.wait(0.1)
							end
							stopConn:Disconnect()
						else
							track:Stop(0.05)
						end
					end
					for _, track in ipairs(tracks) do
						local destroyTrack = track.Destroy
						if destroyTrack then
							destroyTrack(track)
						end
					end
				end
		end
		self:_resetRigPose(rigModel)
		self:_flushAnimatorPose(animatorToStop)
		task.wait()
	end)

	self:_disconnectConnection(heartbeatToDisconnect)

	if not success then
		warn("Error during animation cleanup:", err)
	end
end

function PlaybackService:stopAnimationAndDisconnect(options: StopOptions?)
	self:_cancelDelayedReplay()

	local doInBackground = false
	if options and options.background then
		doInBackground = true
	end

	local animatorToStop = if options and options.animatorOverride then options.animatorOverride else self.State.activeAnimator
	local currentTrack = self.State.currentAnimTrack :: TrackLike?
	local heartbeatToDisconnect = self.State.heartbeat.conn
	local rigModel = self.State.activeRigModel or self.State.lastKnownRigModel
	self._playbackToken = (self._playbackToken :: number) + 1

	local immediateTracks: { TrackLike } = {}
	local immediateTrackSet: TrackSet = {}
	if currentTrack then
		table.insert(immediateTracks, currentTrack)
		immediateTrackSet[currentTrack] = true
	end
	local animator = self:_getAnimatorInstance(animatorToStop)
	if animator then
		local ok, tracks = pcall(function(): { TrackLike }
			return animator:GetPlayingAnimationTracks()
		end)
		if ok and tracks then
			for _, track in ipairs(tracks) do
				if track ~= currentTrack then
					table.insert(immediateTracks, track)
					immediateTrackSet[track] = true
				end
			end
		end
	end

	for _, track in ipairs(immediateTracks) do
		pcall(function()
			track:AdjustSpeed(0)
		end)
		pcall(function()
			track:Stop(0)
		end)
	end

	-- Immediately clear the state and cancel any pending playback callbacks.
	self.State.currentAnimTrack = nil
	self.State.heartbeat.conn = nil
	self.State.isPlaying:set(false)
	self.State.isFinished:set(false)

	self:_disconnectConnection(heartbeatToDisconnect)

	self:_resetRigPose(rigModel)
	self:_flushAnimatorPose(animatorToStop)

	if not animatorToStop and not heartbeatToDisconnect then
		return
	end

	local function cleanupTask()
		self:_cleanupAnimation(animatorToStop, heartbeatToDisconnect, rigModel, immediateTrackSet)
	end

	if doInBackground then
		task.spawn(cleanupTask)
	else
		cleanupTask()
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
	if self.State.currentAnimTrack and self.State.animationLength:get() ~= nil then
		local animTrack = self.State.currentAnimTrack :: AnimationTrack
		local clampedTimePosition = math.clamp(timePosition, 0, animTrack.Length - 0.001)

		animTrack.TimePosition = clampedTimePosition
	else
		warn("There's nothing to seek, import animation data.")
	end
end

function PlaybackService:onPlayPauseButtonActivated()
	if self.State.isPlaying:get() then
		-- Currently playing, pause it
		self.State.isPlaying:set(false)
		if self.State.currentAnimTrack then
			(self.State.currentAnimTrack :: AnimationTrack):AdjustSpeed(0)
		end
	else
		-- Not playing, start playing forward
		self.State.isPlaying:set(true)
		self.State.isReversed:set(false)
		if self.State.isFinished:get() then
			self.State.isFinished:set(false)
			self:seekAnimationToTime(0)
		end
		if self.State.currentAnimTrack then
			(self.State.currentAnimTrack :: AnimationTrack):AdjustSpeed(1)
		end
	end
	self:updateUI()
end

function PlaybackService:onReverseButtonActivated()
	if self.State.isPlaying:get() and self.State.isReversed:get() then
		-- Currently playing in reverse, stop it
		self.State.isPlaying:set(false)
		if self.State.currentAnimTrack then
			(self.State.currentAnimTrack :: AnimationTrack):AdjustSpeed(0)
		end
	else
		-- Start playing in reverse
		self.State.isPlaying:set(true)
		self.State.isReversed:set(true)
		if self.State.playhead:get() == 0 and self.State.animationLength:get() then
			self:seekAnimationToTime(self.State.animationLength:get())
		end
		if self.State.currentAnimTrack then
			(self.State.currentAnimTrack :: AnimationTrack):AdjustSpeed(-1)
		end
	end
	self:updateUI()
end

function PlaybackService:onSliderChange(newValue: number)
	if self.State.currentAnimTrack then
		local wasPlaying = self.State.isPlaying:get()
		local wasReversed = self.State.isReversed:get();
		
		-- Pause animation while seeking
		(self.State.currentAnimTrack :: AnimationTrack):AdjustSpeed(0)
		self:seekAnimationToTime(newValue)
		
		-- Resume animation if it was playing
		if wasPlaying then
			(self.State.currentAnimTrack :: AnimationTrack):AdjustSpeed(wasReversed and -1 or 1)
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
		self.State.activeRig.keyframeNames = self.State.keyframeNames:get() :: { KeyframeNameLike }?
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

return PlaybackService
