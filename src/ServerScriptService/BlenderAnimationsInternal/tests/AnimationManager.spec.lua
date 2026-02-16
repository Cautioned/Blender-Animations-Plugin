return function()
	local AnimationManager = require(script.Parent.Parent.Services.AnimationManager)

	local sampleAxisValue = AnimationManager._testing.sampleAxisValue
	local interpolateMissingAxis = AnimationManager._testing.interpolateMissingAxis
	local ensureChannelSample = AnimationManager._testing.ensureChannelSample

	describe("AnimationManager internals", function()
		describe("sampleAxisValue", function()
			it("should return nil for nil series", function()
				expect(sampleAxisValue(nil, 0.5)).to.never.be.ok()
			end)

			it("should return nil for empty series", function()
				expect(sampleAxisValue({}, 0.5)).to.never.be.ok()
			end)

			it("should return exact value at exact time", function()
				local series = {
					{ time = 0, value = 10 },
					{ time = 1, value = 20 },
				}
				expect(sampleAxisValue(series, 0)).to.be.near(10, 0.001)
				expect(sampleAxisValue(series, 1)).to.be.near(20, 0.001)
			end)

			it("should linearly interpolate between two entries", function()
				local series = {
					{ time = 0, value = 0 },
					{ time = 1, value = 10 },
				}
				expect(sampleAxisValue(series, 0.5)).to.be.near(5, 0.001)
				expect(sampleAxisValue(series, 0.25)).to.be.near(2.5, 0.001)
				expect(sampleAxisValue(series, 0.75)).to.be.near(7.5, 0.001)
			end)

			it("should clamp to last value when past end", function()
				local series = {
					{ time = 0, value = 5 },
					{ time = 1, value = 15 },
				}
				expect(sampleAxisValue(series, 2.0)).to.be.near(15, 0.001)
				expect(sampleAxisValue(series, 100)).to.be.near(15, 0.001)
			end)

			it("should return first value when before first entry", function()
				local series = {
					{ time = 1, value = 42 },
					{ time = 2, value = 84 },
				}
				-- time 0 is before the first entry at time 1
				expect(sampleAxisValue(series, 0)).to.be.near(42, 0.001)
			end)

			it("should handle single-entry series", function()
				local series = { { time = 0.5, value = 7 } }
				-- at the entry
				expect(sampleAxisValue(series, 0.5)).to.be.near(7, 0.001)
				-- past the entry
				expect(sampleAxisValue(series, 1.0)).to.be.near(7, 0.001)
				-- before the entry
				expect(sampleAxisValue(series, 0)).to.be.near(7, 0.001)
			end)

			it("should interpolate correctly across multiple segments", function()
				local series = {
					{ time = 0, value = 0 },
					{ time = 1, value = 10 },
					{ time = 3, value = 30 },
				}
				-- mid first segment
				expect(sampleAxisValue(series, 0.5)).to.be.near(5, 0.001)
				-- mid second segment (1->3, value 10->30, at t=2 => 20)
				expect(sampleAxisValue(series, 2)).to.be.near(20, 0.001)
			end)

			it("should handle near-epsilon time match", function()
				local series = {
					{ time = 1, value = 100 },
				}
				-- within 1e-5 tolerance
				expect(sampleAxisValue(series, 1 + 1e-6)).to.be.near(100, 0.001)
			end)

			it("should handle zero-span between entries", function()
				-- two entries at the same time (degenerate)
				local series = {
					{ time = 1, value = 5 },
					{ time = 1, value = 10 },
				}
				-- should not divide by zero; returns prev.value
				local result = sampleAxisValue(series, 1)
				expect(result).to.be.ok()
			end)

			it("should handle negative values", function()
				local series = {
					{ time = 0, value = -10 },
					{ time = 1, value = 10 },
				}
				expect(sampleAxisValue(series, 0.5)).to.be.near(0, 0.001)
			end)

			it("should produce incorrect results for unsorted input (documents assumption)", function()
				-- sampleAxisValue assumes sorted series. mapCurveChannels sorts before
				-- calling. this test documents that unsorted data gives wrong answers
				-- rather than erroring, so callers know they MUST sort first.
				local unsorted = {
					{ time = 1, value = 10 },
					{ time = 0, value = 0 },
				}
				-- at t=0.5, correct answer would be 5 (linear interp 0->10)
				-- but with unsorted data, it sees time=1 first, which is > 0.5,
				-- and i==1 so it returns entry.value (10) — wrong but not crashing
				local result = sampleAxisValue(unsorted, 0.5)
				expect(result).to.be.ok()
				expect(result).to.never.equal(5) -- documents that it's NOT correct
			end)
		end)

		describe("interpolateMissingAxis", function()
			it("should fill missing axes from timeline", function()
				local axisTimelines = {
					Torso = {
						Position = {
							X = { { time = 0, value = 1 }, { time = 1, value = 5 } },
							Y = { { time = 0, value = 2 } },
							Z = {},
						},
						Rotation = {
							X = { { time = 0, value = 0.1 } },
							Y = {},
							Z = {},
						},
					},
				}

				local finalValues = {}
				interpolateMissingAxis(finalValues, "Torso", 0.5, axisTimelines)

				-- PX interpolated between 1 and 5 at t=0.5 => 3
				expect(finalValues.PX).to.be.near(3, 0.001)
				-- PY only has t=0, so past it => 2
				expect(finalValues.PY).to.be.near(2, 0.001)
				-- PZ empty series => 0
				expect(finalValues.PZ).to.equal(0)
				-- RX single entry => 0.1
				expect(finalValues.RX).to.be.near(0.1, 0.001)
				-- RY, RZ empty => 0
				expect(finalValues.RY).to.equal(0)
				expect(finalValues.RZ).to.equal(0)
			end)

			it("should not overwrite existing values", function()
				local axisTimelines = {
					Arm = {
						Position = {
							X = { { time = 0, value = 99 } },
							Y = {},
							Z = {},
						},
						Rotation = { X = {}, Y = {}, Z = {} },
					},
				}

				local finalValues = { PX = 42 }
				interpolateMissingAxis(finalValues, "Arm", 0, axisTimelines)

				-- PX was already set, should not be overwritten
				expect(finalValues.PX).to.equal(42)
			end)

			it("should default to 0 for unknown pose name", function()
				local finalValues = {}
				interpolateMissingAxis(finalValues, "NonexistentBone", 0, {})

				expect(finalValues.PX).to.equal(0)
				expect(finalValues.PY).to.equal(0)
				expect(finalValues.PZ).to.equal(0)
				expect(finalValues.RX).to.equal(0)
				expect(finalValues.RY).to.equal(0)
				expect(finalValues.RZ).to.equal(0)
			end)
		end)

		describe("applyBoneWeights", function()
			local applyBoneWeights = AnimationManager._testing.applyBoneWeights

			-- Helper to build a minimal KFS with one keyframe containing named poses
			local function buildKFS(poseEntries)
				local kfs = Instance.new("KeyframeSequence")
				local kf = Instance.new("Keyframe")
				kf.Time = 0
				kf.Parent = kfs

				for _, entry in ipairs(poseEntries) do
					local pose = Instance.new("Pose")
					pose.Name = entry.name
					pose.Weight = entry.weight
					pose.CFrame = entry.cframe or CFrame.new()
					pose.Parent = kf
				end

				return kfs
			end

			-- Helper to build a minimal rig stub with bone enabled/disabled state
			local function makeRig(boneStates)
				local bones = {}
				for name, enabled in pairs(boneStates) do
					bones[name] = { enabled = enabled }
				end
				return { bones = bones }
			end

			it("should preserve original Weight=0 on structural poses when bone is enabled", function()
				local kfs = buildKFS({
					{ name = "RightArm", weight = 0 },
				})
				local rig = makeRig({ RightArm = true })

				applyBoneWeights(kfs, rig)

				local pose = kfs:GetKeyframes()[1]:GetDescendants()[1]
				expect(pose.Weight).to.equal(0)

				kfs:Destroy()
			end)

			it("should preserve original Weight=1 on animated poses when bone is enabled", function()
				local kfs = buildKFS({
					{ name = "RightArm", weight = 1, cframe = CFrame.new(1, 2, 3) },
				})
				local rig = makeRig({ RightArm = true })

				applyBoneWeights(kfs, rig)

				local pose = kfs:GetKeyframes()[1]:GetDescendants()[1]
				expect(pose.Weight).to.equal(1)

				kfs:Destroy()
			end)

			it("should force Weight=0 when bone is disabled, regardless of original weight", function()
				local kfs = buildKFS({
					{ name = "RightArm", weight = 1, cframe = CFrame.new(1, 2, 3) },
				})
				local rig = makeRig({ RightArm = false })

				applyBoneWeights(kfs, rig)

				local pose = kfs:GetKeyframes()[1]:GetDescendants()[1]
				expect(pose.Weight).to.equal(0)

				kfs:Destroy()
			end)

			it("should not touch poses for bones not in the rig", function()
				local kfs = buildKFS({
					{ name = "UnknownBone", weight = 1 },
				})
				local rig = makeRig({}) -- no bones at all

				applyBoneWeights(kfs, rig)

				local pose = kfs:GetKeyframes()[1]:GetDescendants()[1]
				expect(pose.Weight).to.equal(1)

				kfs:Destroy()
			end)

			it("should handle nil rig gracefully", function()
				local kfs = buildKFS({
					{ name = "Arm", weight = 1 },
				})

				-- should not error
				applyBoneWeights(kfs, nil)

				local pose = kfs:GetKeyframes()[1]:GetDescendants()[1]
				expect(pose.Weight).to.equal(1)

				kfs:Destroy()
			end)

			it("should handle mixed enabled/disabled bones in one keyframe", function()
				local kfs = buildKFS({
					{ name = "RightArm", weight = 1, cframe = CFrame.new(1, 0, 0) },
					{ name = "LeftArm", weight = 0 },  -- structural, Weight=0
					{ name = "Head", weight = 1, cframe = CFrame.new(0, 1, 0) },
				})
				local rig = makeRig({
					RightArm = false,  -- user disabled
					LeftArm = true,    -- enabled, structural weight=0 should stay
					Head = true,       -- enabled, animated weight=1 should stay
				})

				applyBoneWeights(kfs, rig)

				local poses = {}
				for _, p in ipairs(kfs:GetKeyframes()[1]:GetDescendants()) do
					if p:IsA("Pose") then
						poses[p.Name] = p.Weight
					end
				end

				expect(poses["RightArm"]).to.equal(0)  -- disabled → forced 0
				expect(poses["LeftArm"]).to.equal(0)   -- enabled, original 0 preserved
				expect(poses["Head"]).to.equal(1)       -- enabled, original 1 preserved

				kfs:Destroy()
			end)
		end)

		describe("ensureChannelSample", function()
			it("should create nested structure on first call", function()
				local poseMap = {}
				local result = ensureChannelSample(poseMap, "Torso", 0.5)

				expect(result).to.be.ok()
				expect(result.Position).to.be.ok()
				expect(result.Rotation).to.be.ok()
				expect(poseMap["Torso"]).to.be.ok()
				expect(poseMap["Torso"][0.5]).to.equal(result)
			end)

			it("should return existing entry on second call", function()
				local poseMap = {}
				local first = ensureChannelSample(poseMap, "Torso", 0.5)
				first.Position.X = 42

				local second = ensureChannelSample(poseMap, "Torso", 0.5)
				expect(second.Position.X).to.equal(42)
				expect(second).to.equal(first)
			end)

			it("should keep separate entries for different times", function()
				local poseMap = {}
				local a = ensureChannelSample(poseMap, "Torso", 0)
				local b = ensureChannelSample(poseMap, "Torso", 1)

				expect(a).to.never.equal(b)
			end)

			it("should keep separate entries for different poses", function()
				local poseMap = {}
				local a = ensureChannelSample(poseMap, "Torso", 0)
				local b = ensureChannelSample(poseMap, "Head", 0)

				expect(a).to.never.equal(b)
			end)
		end)
	end)
end
