return function()
	local State = require(script.Parent.Parent.state)
	local PlaybackService = require(script.Parent.Parent.Services.PlaybackService)
	local Types = require(script.Parent.Parent.types)

	describe("PlaybackService", function()
		local playback
		local mockRig

		local function buildRigWithJoints()
			local rig = Instance.new("Model")
			rig.Name = "TestRig"
			rig.Parent = workspace

			local torso = Instance.new("Part")
			torso.Name = "Torso"
			torso.Parent = rig
			rig.PrimaryPart = torso

			local leftArm = Instance.new("Part")
			leftArm.Name = "LeftArm"
			leftArm.Parent = rig

			local rightArm = Instance.new("Part")
			rightArm.Name = "RightArm"
			rightArm.Parent = rig

			local motor1 = Instance.new("Motor6D")
			motor1.Name = "LeftShoulder"
			motor1.Part0 = torso
			motor1.Part1 = leftArm
			motor1.Parent = torso

			local motor2 = Instance.new("Motor6D")
			motor2.Name = "RightShoulder"
			motor2.Part0 = torso
			motor2.Part1 = rightArm
			motor2.Parent = torso

			return rig, { motor1, motor2 }
		end

		beforeEach(function()
			playback = PlaybackService.new(State, Types)
			mockRig, _ = buildRigWithJoints()
			State.activeRigModel = mockRig
			State.lastKnownRigModel = mockRig
			State.heartbeat = { conn = nil }
			State.currentAnimTrack = nil
			State.activeAnimator = nil
		end)

		afterEach(function()
			if mockRig then
				mockRig:Destroy()
			end
			State.activeRigModel = nil
			State.lastKnownRigModel = nil
			State.currentAnimTrack = nil
			State.activeAnimator = nil
			State.heartbeat = { conn = nil }
			State.isPlaying:set(false)
			State.isFinished:set(false)
			State.isReversed:set(false)
			State.isPlaying:set(false)
			State.isFinished:set(false)
			State.isReversed:set(false)
		end)

		describe("stopAnimationAndDisconnect", function()
			it("should reset Motor6D transforms to identity synchronously", function()
				local _, motors = buildRigWithJoints()
				-- use the rig we just built
				local rig = motors[1].Parent.Parent :: Model
				State.activeRigModel = rig

				-- dirty the transforms
				for _, motor in motors do
					motor.Transform = CFrame.new(1, 2, 3) * CFrame.Angles(0.5, 0.5, 0.5)
				end

				playback:stopAnimationAndDisconnect()

				for _, motor in motors do
					-- each component should be identity (within tolerance)
					local pos = motor.Transform.Position
					expect(pos.X).to.be.near(0, 0.001)
					expect(pos.Y).to.be.near(0, 0.001)
					expect(pos.Z).to.be.near(0, 0.001)
				end

				rig:Destroy()
			end)

			it("should reset Bone transforms to identity synchronously", function()
				local rig = Instance.new("Model")
				rig.Name = "DeformRig"
				rig.Parent = workspace

				local meshPart = Instance.new("MeshPart")
				meshPart.Name = "Body"
				meshPart.Parent = rig
				rig.PrimaryPart = meshPart

				local bone1 = Instance.new("Bone")
				bone1.Name = "Spine"
				bone1.Transform = CFrame.new(5, 5, 5)
				bone1.Parent = meshPart

				local bone2 = Instance.new("Bone")
				bone2.Name = "Head"
				bone2.Transform = CFrame.new(10, 10, 10)
				bone2.Parent = bone1

				State.activeRigModel = rig

				playback:stopAnimationAndDisconnect()

				expect(bone1.Transform.Position.X).to.be.near(0, 0.001)
				expect(bone2.Transform.Position.X).to.be.near(0, 0.001)

				rig:Destroy()
			end)

			it("should reset both Motor6Ds and Bones in a mixed rig", function()
				local rig = Instance.new("Model")
				rig.Name = "HybridRig"
				rig.Parent = workspace

				local torso = Instance.new("Part")
				torso.Name = "Torso"
				torso.Parent = rig
				rig.PrimaryPart = torso

				local arm = Instance.new("Part")
				arm.Name = "Arm"
				arm.Parent = rig

				local motor = Instance.new("Motor6D")
				motor.Name = "Shoulder"
				motor.Part0 = torso
				motor.Part1 = arm
				motor.Transform = CFrame.new(7, 7, 7)
				motor.Parent = torso

				local mesh = Instance.new("MeshPart")
				mesh.Name = "Face"
				mesh.Parent = rig

				local bone = Instance.new("Bone")
				bone.Name = "Jaw"
				bone.Transform = CFrame.new(4, 4, 4)
				bone.Parent = mesh

				State.activeRigModel = rig

				playback:stopAnimationAndDisconnect()

				expect(motor.Transform.Position.X).to.be.near(0, 0.001)
				expect(bone.Transform.Position.X).to.be.near(0, 0.001)

				rig:Destroy()
			end)

			it("should reset AnimationConstraint transforms to identity synchronously", function()
				local rig = Instance.new("Model")
				rig.Name = "ConstraintRig"
				rig.Parent = workspace

				local torso = Instance.new("Part")
				torso.Name = "Torso"
				torso.Parent = rig
				rig.PrimaryPart = torso

				local arm = Instance.new("Part")
				arm.Name = "Arm"
				arm.Parent = rig

				local attachment0 = Instance.new("Attachment")
				attachment0.Name = "Shoulder0"
				attachment0.Parent = torso

				local attachment1 = Instance.new("Attachment")
				attachment1.Name = "Shoulder1"
				attachment1.Parent = arm

				local joint = Instance.new("AnimationConstraint")
				joint.Name = "Shoulder"
				joint.Attachment0 = attachment0
				joint.Attachment1 = attachment1
				joint.Transform = CFrame.new(6, 6, 6) * CFrame.Angles(0.2, 0.3, 0.4)
				joint.Parent = torso

				State.activeRigModel = rig

				playback:stopAnimationAndDisconnect()

				local pos = joint.Transform.Position
				expect(pos.X).to.be.near(0, 0.001)
				expect(pos.Y).to.be.near(0, 0.001)
				expect(pos.Z).to.be.near(0, 0.001)

				rig:Destroy()
			end)

			it("should reset joints even when background = true", function()
				local _, motors = buildRigWithJoints()
				local rig = motors[1].Parent.Parent :: Model
				State.activeRigModel = rig

				for _, motor in motors do
					motor.Transform = CFrame.new(3, 3, 3)
				end

				playback:stopAnimationAndDisconnect({ background = true })

				-- joints must be reset immediately, not deferred
				for _, motor in motors do
					local pos = motor.Transform.Position
					expect(pos.X).to.be.near(0, 0.001)
					expect(pos.Y).to.be.near(0, 0.001)
					expect(pos.Z).to.be.near(0, 0.001)
				end

				rig:Destroy()
			end)

			it("should clear currentAnimTrack", function()
				-- just verify state is cleaned up
				playback:stopAnimationAndDisconnect()
				expect(State.currentAnimTrack).to.never.be.ok()
			end)

			it("should stop the live track synchronously before clearing state", function()
				local stopCalled = false
				local speedAdjusted = false
				State.currentAnimTrack = {
					AdjustSpeed = function(_, speed)
						speedAdjusted = (speed == 0)
					end,
					Stop = function(_, fadeTime)
						stopCalled = (fadeTime == 0)
					end,
				}

				playback:stopAnimationAndDisconnect({ background = true })

				expect(speedAdjusted).to.equal(true)
				expect(stopCalled).to.equal(true)
				expect(State.currentAnimTrack).to.never.be.ok()
			end)

			it("should stop animator tracks even when currentAnimTrack is missing", function()
				local track1Stopped = false
				local track2Stopped = false
				local track1 = {
					AdjustSpeed = function() end,
					Stop = function(_, fadeTime)
						track1Stopped = (fadeTime == 0)
					end,
				}
				local track2 = {
					AdjustSpeed = function() end,
					Stop = function(_, fadeTime)
						track2Stopped = (fadeTime == 0)
					end,
				}
				State.currentAnimTrack = nil
				State.activeAnimator = {
					IsA = function(_, className)
						return className == "Humanoid"
					end,
					FindFirstChildOfClass = function(_, className)
						if className == "Animator" then
							return {
								GetPlayingAnimationTracks = function()
									return { track1, track2 }
								end,
							}
						end
						return nil
					end,
				}

				playback:stopAnimationAndDisconnect({ background = true })

				expect(track1Stopped).to.equal(true)
				expect(track2Stopped).to.equal(true)
			end)

			it("should disconnect heartbeat immediately even when background = true", function()
				local disconnected = false
				State.heartbeat = {
					conn = {
						Connected = true,
						Disconnect = function()
							disconnected = true
						end,
					},
				}

				playback:stopAnimationAndDisconnect({ background = true })

				expect(disconnected).to.equal(true)
				expect(State.heartbeat.conn).to.never.be.ok()
			end)

			it("should clear finished state when stopping animation", function()
				State.isFinished:set(true)

				playback:stopAnimationAndDisconnect({ background = true })

				expect(State.isFinished:get()).to.equal(false)
				expect(State.isPlaying:get()).to.equal(false)
			end)

			it("should reset the last known rig when activeRigModel is already nil", function()
				local _, motors = buildRigWithJoints()
				local rig = motors[1].Parent.Parent :: Model
				State.activeRigModel = nil
				State.lastKnownRigModel = rig

				for _, motor in motors do
					motor.Transform = CFrame.new(8, 8, 8)
				end

				playback:stopAnimationAndDisconnect()

				for _, motor in motors do
					expect(motor.Transform.Position.X).to.be.near(0, 0.001)
				end

				rig:Destroy()
			end)

			it("should flush the animator pose after resetting transforms", function()
				local steppedDelta = nil
				State.activeAnimator = {
					IsA = function(_, className)
						return className == "Humanoid"
					end,
					FindFirstChildOfClass = function(_, className)
						if className == "Animator" then
							return {
								GetPlayingAnimationTracks = function()
									return {}
								end,
								StepAnimations = function(_, delta)
									steppedDelta = delta
								end,
							}
						end
						return nil
					end,
				}

				playback:stopAnimationAndDisconnect()

				expect(steppedDelta).to.equal(0)
			end)

			it("should not error when no rig or animator exists", function()
				State.activeRigModel = nil
				State.activeAnimator = nil
				State.heartbeat = { conn = nil }

				-- should not throw
				expect(function()
					playback:stopAnimationAndDisconnect()
				end).to.never.throw()
			end)

			it("should preserve Motor6D C0 and C1 while resetting Transform", function()
				local rig = Instance.new("Model")
				rig.Name = "C0C1Rig"
				rig.Parent = workspace

				local torso = Instance.new("Part")
				torso.Name = "Torso"
				torso.Parent = rig
				rig.PrimaryPart = torso

				local arm = Instance.new("Part")
				arm.Name = "Arm"
				arm.Parent = rig

				local c0 = CFrame.new(1, 0.5, 0) * CFrame.Angles(0, 0, math.rad(45))
				local c1 = CFrame.new(0, -0.5, 0)
				local motor = Instance.new("Motor6D")
				motor.Name = "Shoulder"
				motor.Part0 = torso
				motor.Part1 = arm
				motor.C0 = c0
				motor.C1 = c1
				motor.Transform = CFrame.new(5, 5, 5)
				motor.Parent = torso

				State.activeRigModel = rig

				playback:stopAnimationAndDisconnect()

				-- Transform zeroed
				expect(motor.Transform.Position.X).to.be.near(0, 0.001)
				-- C0/C1 untouched
				expect(motor.C0.Position.X).to.be.near(c0.Position.X, 0.001)
				expect(motor.C0.Position.Y).to.be.near(c0.Position.Y, 0.001)
				expect(motor.C1.Position.Y).to.be.near(c1.Position.Y, 0.001)

				rig:Destroy()
			end)

			it("should reset deeply nested bones", function()
				local rig = Instance.new("Model")
				rig.Name = "DeepBoneRig"
				rig.Parent = workspace

				local mesh = Instance.new("MeshPart")
				mesh.Name = "Body"
				mesh.Parent = rig
				rig.PrimaryPart = mesh

				-- chain: Root > Spine > Chest > Neck > Head
				local bones = {}
				local parent: Instance = mesh
				for _, name in { "Root", "Spine", "Chest", "Neck", "Head" } do
					local bone = Instance.new("Bone")
					bone.Name = name
					bone.Transform = CFrame.new(1, 2, 3) * CFrame.Angles(0.3, 0.3, 0.3)
					bone.Parent = parent
					table.insert(bones, bone)
					parent = bone
				end

				State.activeRigModel = rig

				playback:stopAnimationAndDisconnect()

				for _, bone in bones do
					expect(bone.Transform.Position.X).to.be.near(0, 0.001)
					expect(bone.Transform.Position.Y).to.be.near(0, 0.001)
					expect(bone.Transform.Position.Z).to.be.near(0, 0.001)
				end

				rig:Destroy()
			end)

			it("should not error on a rig with no joints", function()
				local rig = Instance.new("Model")
				rig.Name = "JointlessRig"
				rig.Parent = workspace

				local part = Instance.new("Part")
				part.Name = "Torso"
				part.Parent = rig
				rig.PrimaryPart = part

				State.activeRigModel = rig

				expect(function()
					playback:stopAnimationAndDisconnect()
				end).to.never.throw()

				rig:Destroy()
			end)

			it("should be idempotent when called twice", function()
				local _, motors = buildRigWithJoints()
				local rig = motors[1].Parent.Parent :: Model
				State.activeRigModel = rig

				for _, motor in motors do
					motor.Transform = CFrame.new(2, 2, 2)
				end

				playback:stopAnimationAndDisconnect()
				-- call again immediately — should not throw or re-dirty
				playback:stopAnimationAndDisconnect()

				for _, motor in motors do
					expect(motor.Transform.Position.X).to.be.near(0, 0.001)
				end

				rig:Destroy()
			end)
		end)

		describe("disconnectHeartbeat", function()
			it("should disconnect an active heartbeat connection", function()
				local connected = true
				local mockConn = {
					Disconnect = function(self)
						connected = false
					end,
					Connected = true,
				}
				State.heartbeat = { conn = mockConn }

				playback:disconnectHeartbeat()

				expect(State.heartbeat.conn).to.never.be.ok()
				expect(connected).to.equal(false)
			end)

			it("should be safe to call with no active connection", function()
				State.heartbeat = { conn = nil }

				expect(function()
					playback:disconnectHeartbeat()
				end).to.never.throw()
			end)
		end)

		describe("delayed replay", function()
			it("should replay an unlooped animation after 1 second", function()
				local playCalls = 0
				local adjustSpeeds = {}
				local mockTrack = {
					Length = 1,
					TimePosition = 1,
					Play = function(self)
						playCalls += 1
						self.TimePosition = 0
					end,
					AdjustSpeed = function(_, speed)
						table.insert(adjustSpeeds, speed)
					end,
				}

				State.currentAnimTrack = mockTrack :: any
				State.animationLength:set(1)
				State.loopAnimation:set(false)
				State.isPlaying:set(true)
				State.heartbeat = { conn = nil }

				playback._playbackToken = 123

				playback:_scheduleDelayedReplay(123, function()
					if State.currentAnimTrack ~= mockTrack then
						return
					end
					mockTrack.TimePosition = 0
					mockTrack:Play()
					mockTrack:AdjustSpeed(1)
					State.isPlaying:set(true)
					State.isReversed:set(false)
					State.isFinished:set(false)
				end)

				task.wait(1.1)

				expect(playCalls).to.equal(1)
				expect(adjustSpeeds[#adjustSpeeds]).to.equal(1)
				expect(State.isPlaying:get()).to.equal(true)
				expect(State.isFinished:get()).to.equal(false)
			end)

			it("should cancel a delayed replay when cleanup runs", function()
				local playCalls = 0
				local mockTrack = {
					AdjustSpeed = function() end,
					Stop = function() end,
					Play = function()
						playCalls += 1
					end,
				}

				State.currentAnimTrack = mockTrack :: any
				playback._playbackToken = 456

				playback:_scheduleDelayedReplay(456, function()
					mockTrack:Play()
				end)

				playback:stopAnimationAndDisconnect()
				task.wait(1.1)

				expect(playCalls).to.equal(0)
			end)
		end)
	end)
end
