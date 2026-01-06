--!strict
-- I moved all the playback logic here to make it easier to manage.
local PlaybackService = {}
PlaybackService.__index = PlaybackService

local RunService = game:GetService("RunService")
local AnimationClipProvider = game:GetService("AnimationClipProvider")
local Utils = require(script.Parent.Parent:WaitForChild("Utils"))

type HeartbeatType = { conn: RBXScriptConnection? }
type StopOptions = { background: boolean?, animatorOverride: Instance? }

function PlaybackService.new(State, Types)
	local self = setmetatable({}, PlaybackService)
	self.State = State
	self.Types = Types
	return self
end

function PlaybackService:disconnectHeartbeat()
	local heartbeat = self.State.heartbeat :: HeartbeatType
	if heartbeat.conn then
		heartbeat.conn:Disconnect()
		heartbeat.conn = nil
	end
end

function PlaybackService:_cleanupAnimation(animatorToStop, heartbeatToDisconnect)
	local success, err = pcall(function()
		if
			animatorToStop
			and typeof(animatorToStop) == "Instance"
			and ((animatorToStop :: any):IsA("Humanoid") or (animatorToStop :: any):IsA("AnimationController"))
		then
			local animator = (animatorToStop :: any):FindFirstChildOfClass("Animator")
			if animator then
				local tracks = (animator :: any):GetPlayingAnimationTracks() :: { any }
				if #tracks > 0 then
					for _, track in ipairs(tracks) do
						track:Stop(0.01)
					end
					task.wait(0.1)
					for _, track in ipairs(tracks) do
						track:Destroy()
					end
				end
			end
		end
		task.wait()
	end)

	if heartbeatToDisconnect and (heartbeatToDisconnect :: any).Connected then
		(heartbeatToDisconnect :: any):Disconnect()
	end

	if not success then
		warn("Error during animation cleanup:", err)
	end
end

function PlaybackService:stopAnimationAndDisconnect(options: StopOptions?)
	local doInBackground = false
	if options and options.background then
		doInBackground = true
	end

	local animatorToStop = if options and options.animatorOverride then options.animatorOverride else self.State.activeAnimator
	local heartbeatToDisconnect = self.State.heartbeat.conn

	-- Immediately clear the state for the new animation, but leave the animator.
	self.State.currentAnimTrack = nil
	self.State.heartbeat.conn = nil

	if not animatorToStop and not heartbeatToDisconnect then
		return
	end

	local function cleanupTask()
		self:_cleanupAnimation(animatorToStop, heartbeatToDisconnect)
	end

	-- I switched to using independent tasks make sure when switching rigs that the old animation is stopped before the new one is started.
	-- Essentially, it's now bulletproof. Very simple, I'm surprised I never thought of this before.

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
            animTrack.TimePosition = 0
            animTrack:Play()
            -- ensure ui reflects the current state
            self.State.isPlaying:set(true)
            self.State.isReversed:set(false)
            self:updateUI()
        end
    end

	playAnimation()

	local lastStepTime = tick()

	self:disconnectHeartbeat()
	self.State.heartbeat.conn = RunService.Heartbeat:Connect(function(step)
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
							self.State.isPlaying:set(false)
							self.State.isFinished:set(true)
							self:updateUI()
							task.spawn(function()
								task.wait(self.State.stopSpeed:get())
								if self.State.isFinished:get() and self.State.currentAnimTrack then
									self.State.isFinished:set(false)
									playAnimation()
									self:updateUI()
								end
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
