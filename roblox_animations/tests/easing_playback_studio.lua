-- Integration test: run from Studio's command bar/plugin context (StepAnimations).
-- Inputs are the production Rig module, an untouched template, and a Blender case.
-- This exercises LoadAnimation -> ToRobloxAnimation -> the real Animator.
return function(Rig, template, case)
	local model = template:Clone()
	model.Name = "__EasingPlaybackTest"
	local sequence, animation, track
	local ok, result = xpcall(function()
		for _, child in model:GetDescendants() do
			if child:IsA("BasePart") then child.Anchored = true end
			if child:IsA("BaseScript") then child:Destroy() end
		end
		local weapon = Instance.new("Part")
		weapon.Name = "VerificationWeapon"
		weapon.Anchored = true
		weapon.CFrame = model[case.weaponParent].CFrame
		weapon.Parent = model
		local grip = Instance.new("Motor6D")
		grip.Name = weapon.Name
		grip.Part0 = model[case.weaponParent]
		grip.Part1 = weapon
		grip.Parent = grip.Part0
		model.Parent = workspace
		local rig = Rig.new(model)
		rig:LoadAnimation(case.baked)
		sequence = rig:ToRobloxAnimation()
		assert(sequence, "No animation was generated")
		sequence.Loop = true
		animation = Instance.new("Animation")
		animation.AnimationId = game:GetService("KeyframeSequenceProvider"):RegisterKeyframeSequence(sequence)
		local animator = model:FindFirstChildWhichIsA("Animator", true)
		if not animator then animator = Instance.new("Animator", model:FindFirstChildOfClass("Humanoid")) end
		track = animator:LoadAnimation(animation)
		track:Play(0, 1, 1)
		local deadline = os.clock() + 5
		repeat task.wait() until track.Length > 0 or os.clock() >= deadline
		assert(track.Length > 0, "Animation did not load; a zero-length track is not a pass")
		assert(math.abs(track.Length - case.baked.t) < 0.0001, "Animation duration changed")
		track.TimePosition = 0
		local joints = {}
		for _, child in model:GetDescendants() do
			if child:IsA("Motor6D") then table.insert(joints, child) end
		end
		local report = {id=case.id, samples=0, failed=0, maxPosition=0, maxAngle=0, maxKeyPosition=0, maxRepeatDrift=0, maxTimeDrift=0}
		local firstCycle = {}
		local lastTime = 0
		for cycle=0,2 do
			for index, sample in case.expected do
				-- Evaluate just after an exact key to avoid floating-point ambiguity
				-- at Constant discontinuities after repeated time accumulation.
				local time = sample.t + cycle * case.baked.t + (if case.interpolation == "CONSTANT" then 0.00002 else 0)
				animator:StepAnimations(time - lastTime)
				lastTime = time
                local phase = time % case.baked.t
                local timeError = math.abs(track.TimePosition - phase)
                timeError = math.min(timeError, math.abs(timeError - case.baked.t))
                report.maxTimeDrift = math.max(report.maxTimeDrift, timeError)
                -- Compare poses at the same phase. Accumulated float time near
                -- a discontinuity is measured separately, not as spatial drift.
                track.TimePosition = phase
                animator:StepAnimations(0)
				local world = {HumanoidRootPart=model.HumanoidRootPart.CFrame}
				local pending = table.clone(joints)
				while #pending > 0 do
					local progressed = false
					for i=#pending,1,-1 do
						local joint = pending[i]
						if world[joint.Part0.Name] then
							world[joint.Part1.Name] = world[joint.Part0.Name] * joint.C0 * joint.Transform * joint.C1:Inverse()
							table.remove(pending, i)
							progressed = true
						end
					end
					assert(progressed, "Disconnected Motor6D hierarchy")
				end
				firstCycle[index] = firstCycle[index] or {}
				for bone, components in sample.poses do
					local actual = assert(world[bone], "Missing bone " .. bone)
					local expected = CFrame.new(table.unpack(components))
					local difference = expected:ToObjectSpace(actual)
					local position = difference.Position.Magnitude
					local _, radians = difference:ToAxisAngle()
					local angle = math.deg(math.abs(radians))
					assert(position == position and angle == angle, "Non-finite pose")
					report.samples += 1
					report.maxPosition = math.max(report.maxPosition, position)
					report.maxAngle = math.max(report.maxAngle, angle)
					if sample.onFrame then report.maxKeyPosition = math.max(report.maxKeyPosition, position) end
					local positionLimit = if sample.onFrame then 0.001 else 0.01
					local angleLimit = if sample.onFrame then 0.1 else 0.5
					if position > positionLimit or angle > angleLimit then
						report.failed += 1
						if not report.firstFailure then report.firstFailure={time=sample.t,cycle=cycle,bone=bone,position=position,angle=angle,onFrame=sample.onFrame} end
					end
					if cycle == 0 then firstCycle[index][bone] = actual else
						report.maxRepeatDrift = math.max(report.maxRepeatDrift, (actual.Position - firstCycle[index][bone].Position).Magnitude)
					end
				end
			end
		end
		-- Also let the clock run freely for three complete loops. The pose
		-- checks above seek to an exact phase so sharp curves do not mistake
		-- floating-point clock error for transform error.
		track.TimePosition = 0
		local steps = math.ceil(case.baked.t * 3 * 60)
		for step=1,steps do
			animator:StepAnimations(1 / 60)
			local phase = (step / 60) % case.baked.t
			local error = math.abs(track.TimePosition - phase)
			error = math.min(error, math.abs(error - case.baked.t))
			report.maxTimeDrift = math.max(report.maxTimeDrift, error)
		end
		report.pass = report.failed == 0 and report.maxRepeatDrift < 0.001 and report.maxTimeDrift < 0.0001
		return report
	end, debug.traceback)
	if track then track:Stop(0) track:Destroy() end
	if animation then animation:Destroy() end
	if sequence then sequence:Destroy() end
	model:Destroy()
	if not ok then return {id=case.id, pass=false, error=result} end
	return result
end
