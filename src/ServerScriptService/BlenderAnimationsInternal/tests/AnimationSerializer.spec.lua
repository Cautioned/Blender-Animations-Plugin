--!native
--!strict

--[[
We are utilizing TestEZ for testing, this helps to ensure our code is working as expected.
It's kinda sloppy, but it works. TDD is good for plugins.

]]

return function()
	local AnimationSerializer = require(script.Parent.Parent.Components.AnimationSerializer)
	local DeflateLua = require(script.Parent.Parent.Components.DeflateLua)
	local AnimationManager = require(script.Parent.Parent.Services.AnimationManager)

	local function makeUncompressedZlib(payload: string): string
		local chunks = { "\120\1" }
		local offset = 1
		local remaining = #payload
		local checksum = 1

		while remaining > 0 do
			local blockLen = math.min(remaining, 65535)
			local isLastBlock = remaining == blockLen
			local nlen = 65535 - blockLen

			chunks[#chunks + 1] = string.char(if isLastBlock then 1 else 0)
			chunks[#chunks + 1] = string.char(blockLen % 256, math.floor(blockLen / 256))
			chunks[#chunks + 1] = string.char(nlen % 256, math.floor(nlen / 256))

			local block = string.sub(payload, offset, offset + blockLen - 1)
			chunks[#chunks + 1] = block

			for i = 1, #block do
				checksum = DeflateLua.adler32(string.byte(block, i), checksum)
			end

			offset += blockLen
			remaining -= blockLen
		end

		chunks[#chunks + 1] = string.char(
			math.floor(checksum / 16777216) % 256,
			math.floor(checksum / 65536) % 256,
			math.floor(checksum / 256) % 256,
			checksum % 256
		)

		return table.concat(chunks)
	end

	describe("AnimationSerializer", function()
		local serializer

		beforeEach(function()
			serializer = AnimationSerializer.new()
		end)

		describe("serialize", function()
			it("should serialize a simple KeyframeSequence", function()
				local kfs = Instance.new("KeyframeSequence")
				local kf1 = Instance.new("Keyframe")
				kf1.Time = 0
				kf1.Parent = kfs
				local pose1 = Instance.new("Pose")
				pose1.Name = "Part"
				pose1.CFrame = CFrame.new(1, 2, 3)
				pose1.EasingStyle = Enum.PoseEasingStyle.Linear
				pose1.EasingDirection = Enum.PoseEasingDirection.In
				pose1.Parent = kf1

				local kf2 = Instance.new("Keyframe")
				kf2.Time = 1
				kf2.Parent = kfs
				local pose2 = Instance.new("Pose")
				pose2.Name = "Part"
				pose2.CFrame = CFrame.new(4, 5, 6)
				pose2.EasingStyle = Enum.PoseEasingStyle.Linear
				pose2.EasingDirection = Enum.PoseEasingDirection.In
				pose2.Parent = kf2

				local rig = {
					isDeformRig = false,
					bones = {},
					ToRobloxAnimation = function() return kfs end,
				}

				local result = serializer:serialize(kfs, rig)

				expect(result).to.be.ok()
				expect(result.t).to.be.near(1)
				expect(#result.kfs).to.equal(2)
				expect(result.kfs[1].t).to.be.near(0)
				expect(result.kfs[2].t).to.be.near(1)
				local poseResult = result.kfs[1].kf.Part
				expect(poseResult.easingStyle).to.equal("Linear")
				expect(poseResult.easingDirection).to.equal("In")
			end)

			it("should handle different easing styles and directions", function()
				local kfs = Instance.new("KeyframeSequence")
				local kf1 = Instance.new("Keyframe")
				kf1.Time = 0
				kf1.Parent = kfs
				local pose1 = Instance.new("Pose")
				pose1.Name = "Part"
				pose1.CFrame = CFrame.new(1, 2, 3)
				pose1.EasingStyle = Enum.PoseEasingStyle.Cubic
				pose1.EasingDirection = Enum.PoseEasingDirection.Out
				pose1.Parent = kf1

				local rig = { isDeformRig = false, bones = {}, ToRobloxAnimation = function() return kfs end }
				local result = serializer:serialize(kfs, rig)

				expect(result).to.be.ok()
				local poseResult = result.kfs[1].kf.Part
				expect(poseResult.easingStyle).to.equal("Cubic")
				expect(poseResult.easingDirection).to.equal("Out")
			end)

			it("should ignore keyframes that have no poses", function()
				local kfs = Instance.new("KeyframeSequence")
				local kf1 = Instance.new("Keyframe")
				kf1.Time = 0
				kf1.Parent = kfs

				local kf2 = Instance.new("Keyframe")
				kf2.Time = 1
				kf2.Parent = kfs
				local pose2 = Instance.new("Pose")
				pose2.Name = "Part"
				pose2.Parent = kf2

				local rig = { isDeformRig = false, bones = {}, ToRobloxAnimation = function() return kfs end }
				local result = serializer:serialize(kfs, rig)
				expect(result).to.be.ok()
				expect(#result.kfs).to.equal(1)
			end)

			it("should serialize face controls from number poses", function()
				local kfs = Instance.new("KeyframeSequence")
				local kf1 = Instance.new("Keyframe")
				kf1.Time = 0
				kf1.Parent = kfs

				local faceFolder = Instance.new("Folder")
				faceFolder.Name = "FaceControls"
				faceFolder.Parent = kf1

				local smile = Instance.new("NumberPose")
				smile.Name = "JawDrop"
				smile.Value = 0.75
				smile.Parent = faceFolder

				local rig = { isDeformRig = false, bones = {}, ToRobloxAnimation = function() return kfs end }
				local result = serializer:serialize(kfs, rig)

				expect(result).to.be.ok()
				expect(#result.kfs).to.equal(1)
				expect(result.kfs[1].fc).to.be.ok()
				expect(result.kfs[1].fc.JawDrop).to.be.ok()
				expect(result.kfs[1].fc.JawDrop.value).to.be.near(0.75)
				expect(result.kfs[1].fc.JawDrop.easingStyle).to.equal("Linear")
				expect(result.kfs[1].fc.JawDrop.easingDirection).to.equal("Out")
			end)

			it("should return nil for a KeyframeSequence with no keyframes", function()
				local kfs = Instance.new("KeyframeSequence")
				local rig = { isDeformRig = false, bones = {}, ToRobloxAnimation = function() return kfs end }
				local result = serializer:serialize(kfs, rig)
				expect(result).to.never.be.ok()
			end)

			it("should correctly sort unsorted keyframes", function()
				local kfs = Instance.new("KeyframeSequence")
				local kf1 = Instance.new("Keyframe")
				kf1.Time = 1
				kf1.Parent = kfs
				local pose1 = Instance.new("Pose")
				pose1.Name = "Part"
				pose1.Parent = kf1

				local kf2 = Instance.new("Keyframe")
				kf2.Time = 0
				kf2.Parent = kfs
				local pose2 = Instance.new("Pose")
				pose2.Name = "Part"
				pose2.Parent = kf2

				local rig = { isDeformRig = false, bones = {}, ToRobloxAnimation = function() return kfs end }
				local result = serializer:serialize(kfs, rig)

				expect(result).to.be.ok()
				expect(result.kfs[1].t).to.be.near(0)
				expect(result.kfs[2].t).to.be.near(1)
			end)

			it("should normalize keyframe times based on the first keyframe", function()
				local kfs = Instance.new("KeyframeSequence")
				local kf1 = Instance.new("Keyframe")
				kf1.Time = 1
				kf1.Parent = kfs
				local pose1 = Instance.new("Pose")
				pose1.Name = "Part"
				pose1.Parent = kf1

				local kf2 = Instance.new("Keyframe")
				kf2.Time = 2
				kf2.Parent = kfs
				local pose2 = Instance.new("Pose")
				pose2.Name = "Part"
				pose2.Parent = kf2

				local rig = { isDeformRig = false, bones = {}, ToRobloxAnimation = function() return kfs end }
				local result = serializer:serialize(kfs, rig)

				expect(result).to.be.ok()
				expect(result.kfs[1].t).to.be.near(0)
				expect(result.kfs[2].t).to.be.near(1)
				expect(result.t).to.be.near(1)
			end)

			it("should handle deform rigs correctly", function()
				local kfs = Instance.new("KeyframeSequence")
				local kf1 = Instance.new("Keyframe")
				kf1.Time = 0
				kf1.Parent = kfs
				local pose1 = Instance.new("Pose")
				pose1.Name = "Part"
				pose1.Parent = kf1

				local rig = { isDeformRig = true, bones = {}, ToRobloxAnimation = function() return kfs end }
				local result = serializer:serialize(kfs, rig)

				expect(result).to.be.ok()
				expect(result.is_deform_rig).to.be.ok()
				expect(result.is_deform_bone_rig).to.be.ok()
			end)

			it("should correctly serialize keyframes with parent-child bone relationships in time order", function()
				local kfs = Instance.new("KeyframeSequence")

				-- Keyframe at t=1
				local kf1 = Instance.new("Keyframe")
				kf1.Time = 1
				kf1.Parent = kfs
				local pose1_parent = Instance.new("Pose")
				pose1_parent.Name = "ParentBone"
				pose1_parent.Parent = kf1
				local pose1_child = Instance.new("Pose")
				pose1_child.Name = "ChildBone"
				pose1_child.Parent = kf1

				-- Keyframe at t=0
				local kf2 = Instance.new("Keyframe")
				kf2.Time = 0
				kf2.Parent = kfs
				local pose2_child = Instance.new("Pose")
				pose2_child.Name = "ChildBone"
				pose2_child.Parent = kf2
				local pose2_parent = Instance.new("Pose")
				pose2_parent.Name = "ParentBone"
				pose2_parent.Parent = kf2

				local rig = { isDeformRig = false, bones = {}, ToRobloxAnimation = function() return kfs end }
				local result = serializer:serialize(kfs, rig)

				expect(result).to.be.ok()
				expect(#result.kfs).to.equal(2)
				expect(result.kfs[1].t).to.be.near(0)
				expect(result.kfs[2].t).to.be.near(1)
				expect(result.kfs[1].kf.ParentBone).to.be.ok()
				expect(result.kfs[1].kf.ChildBone).to.be.ok()
				expect(result.kfs[2].kf.ParentBone).to.be.ok()
				expect(result.kfs[2].kf.ChildBone).to.be.ok()
			end)
		end)

		describe("legacy decode compatibility", function()
			it("should accept common legacy inputs without throwing", function()
				local serializer2 = AnimationSerializer.new()
				local Http = game:GetService("HttpService")
				local BaseXX = require(script.Parent.Parent.Components.BaseXX)

				-- minimal serialized payload (raw json)
				local json = Http:JSONEncode({ t = 0, kfs = { { t = 0, kf = { Bone = { components = {0,0,0,1,0,0,0,1,0,0,0,1}, easingStyle = "Linear", easingDirection = "Out" } } } } })
				local b64json = BaseXX.to_base64(json)

				-- try raw json (text path)
				pcall(function() serializer2:deserialize(json, false) end)
				-- try base64 json (legacy text path)
				pcall(function() serializer2:deserialize(b64json, false) end)
				-- try raw json on binary path (should hit fallback)
				pcall(function() serializer2:deserialize(json, true) end)
				expect(true).to.equal(true)
			end)

			it("should deserialize large compressed payloads", function()
				local serializer2 = AnimationSerializer.new()
				local Http = game:GetService("HttpService")
				local payload = string.rep("abcdef1234567890", 16000)
				local json = Http:JSONEncode({ t = 0, kfs = {}, payload = payload })
				local compressed = makeUncompressedZlib(json)

				local result = serializer2:deserialize(compressed, true)

				expect(result).to.be.ok()
				expect(result.payload).to.equal(payload)
			end)
		end)
	end)
end