return function()
	local root = script.Parent.Parent
	local State = require(root.state)
	local Rig = require(root.Components.Rig)
	local Utils = require(root.Utils)
	local AnimationManager = require(root.Services.AnimationManager)

	describe("Animation Resizer import", function()
		local model, rig, manager, raw, preview
		local sequences, saved, values
		local valueKeys = {
			"scaleFactor",
			"loopAnimation",
			"selectedPriority",
			"keyframeNames",
			"animationLength",
			"keyframeStats",
			"lastRawAnimData",
			"currentAnimationData",
			"mirrorAnimationEnabled",
			"speedEnabled",
			"resampleEnabled",
			"simplifierEnabled",
		}
		local function poseAt(sequence, time)
			for _, frame in sequence:GetKeyframes() do
				if math.abs(frame.Time - time) < 0.00001 then
					return frame:FindFirstChild("Limb", true)
				end
			end
			error("Missing sample")
		end
		local function expectCFrame(actual, expected)
			local delta = expected:ToObjectSpace(actual)
			expect(delta.Position.Magnitude).to.be.near(0, 0.0001)
			local _, angle = delta:ToAxisAngle()
			expect(angle).to.be.near(0, 0.001)
		end
		local function expectScale(factor)
			local source = rig:ToRobloxAnimation()
			table.insert(sequences, source)
			local original = CFrame.new(2, -3, 4) * CFrame.Angles(0.2, 0.4, -0.1)
			expectCFrame(poseAt(source, 0).CFrame, original)
			expectCFrame(poseAt(preview, 0).CFrame, CFrame.new(original.Position * factor) * original.Rotation)
			expect(poseAt(preview, 0).Weight).to.equal(1)
			expect(preview:GetKeyframes()[2].Time).to.equal(1)
			expect(raw.kfs[1].kf.Limb[1][1]).to.equal(2)
			local exported = manager:createKeyframeSequenceFromState()
			table.insert(sequences, exported)
			expectCFrame(poseAt(exported, 0).CFrame, poseAt(preview, 0).CFrame)
		end
		beforeEach(function()
			sequences = {}
			saved =
				{ activeRig = State.activeRig, activeRigModel = State.activeRigModel, animationData = State.animationData }
			values = {}
			for _, key in valueKeys do
				values[key] = { State[key]:get() }
			end
			State.mirrorAnimationEnabled:set(false)
			State.speedEnabled:set(false)
			State.resampleEnabled:set(false)
			State.simplifierEnabled:set(false)
			State.keyframeNames:set({})
			model = Instance.new("Model")
			local base = Instance.new("Part", model)
			base.Name = "RootPart"
			local limb = Instance.new("Part", model)
			limb.Name = "Limb"
			local motor = Instance.new("Motor6D", base)
			motor.Part0 = base
			motor.Part1 = limb
			model.PrimaryPart = base
			rig = Rig.new(model)
			State.activeRig = rig
			State.activeRigModel = model
			local cf = CFrame.new(2, -3, 4) * CFrame.Angles(0.2, 0.4, -0.1)
			raw = {
				t = 1,
				kfs = {
					{ t = 0, kf = { Limb = { { cf:GetComponents() }, "Linear", "Out" } } },
					{ t = 1, kf = { Limb = { { CFrame.new(4, 1, -2):GetComponents() }, "Linear", "Out" } } },
				},
			}
			manager = AnimationManager.new({
				playAllRigs = function()
					local source = rig:ToRobloxAnimation()
					table.insert(sequences, source)
					preview = Utils.scaleAnimation(source, State.scaleFactor:get())
					table.insert(sequences, preview)
				end,
			})
			manager.animationSerializerService.deserialize = function()
				return raw
			end
		end)
		afterEach(function()
			for _, sequence in sequences do
				sequence:Destroy()
			end
			model:Destroy()
			State.activeRig = saved.activeRig
			State.activeRigModel = saved.activeRigModel
			State.animationData = saved.animationData
			for _, key in valueKeys do
				State[key]:set(values[key][1])
			end
		end)
		for _, factor in { 0.5, 1, 2, 3 } do
			it("applies factor " .. factor .. " once on the first import", function()
				State.scaleFactor:set(factor)
				manager:loadAnim("fixture", false)
				expect(manager:loadRig()).to.equal(true)
				expectScale(factor)
			end)
		end
		it("does not compound when rebuilding the preview repeatedly", function()
			State.scaleFactor:set(2)
			manager:loadAnim("fixture", false)
			for _ = 1, 3 do
				manager:loadRig()
				expectScale(2)
			end
		end)
		it("matches toggling off and back on without requiring the toggle", function()
			State.scaleFactor:set(2)
			manager:loadAnim("fixture", false)
			manager:loadRig()
			local first = poseAt(preview, 0).CFrame
			State.scaleFactor:set(1)
			manager:resimplifyAndPlay()
			expectScale(1)
			State.scaleFactor:set(2)
			manager:resimplifyAndPlay()
			expectScale(2)
			expectCFrame(first, poseAt(preview, 0).CFrame)
		end)
		it("does not carry resized poses into the next imported clip", function()
			State.scaleFactor:set(0.5)
			for _ = 1, 3 do
				manager:loadAnim("fixture", false)
				manager:loadRig()
				expectScale(0.5)
			end
		end)
		it("keeps saved sequences unchanged while resizing their preview", function()
			State.scaleFactor:set(2)
			rig:LoadAnimation(raw)
			local savedSequence = rig:ToRobloxAnimation()
			table.insert(sequences, savedSequence)
			local original = poseAt(savedSequence, 0).CFrame
			manager:playSavedAnimation({ instance = savedSequence })
			expectScale(2)
			expectCFrame(poseAt(savedSequence, 0).CFrame, original)
		end)
		it("keeps live sync source poses unscaled", function()
			State.scaleFactor:set(2)
			expect(manager:applyLiveSyncEnvelope({ type = "animation_full", hash = "new", animation = raw }, "old")).to.equal(
				true
			)
			expectScale(2)
		end)
	end)
end
