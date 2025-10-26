return function()
	local testez = require(script.Parent.Parent.Components.testez)
	local rig_module = require(script.Parent.Parent.Components.Rig)
	local RigManager = require(script.Parent.Parent.Services.RigManager)
	local BlenderConnection = require(script.Parent.Parent.Components.BlenderConnection)
	local State = require(script.Parent.Parent.state)

	describe("Rig Module", function()
		local mock_rig
		beforeEach(function()
			mock_rig = Instance.new("Model")
			mock_rig.Name = "MockRig"

			local hrp = Instance.new("Part")
			hrp.Name = "HumanoidRootPart"
			hrp.Parent = mock_rig
			mock_rig.PrimaryPart = hrp

			local torso = Instance.new("Part")
			torso.Name = "Torso"
			torso.Parent = mock_rig

			local head = Instance.new("Part")
			head.Name = "Head"
			head.Parent = mock_rig

			local torso_motor = Instance.new("Motor6D")
			torso_motor.Name = "TorsoMotor"
			torso_motor.Part0 = hrp
			torso_motor.Part1 = torso
			torso_motor.Parent = hrp

			local head_motor = Instance.new("Motor6D")
			head_motor.Name = "HeadMotor"
			head_motor.Part0 = torso
			head_motor.Part1 = head
			head_motor.Parent = torso
		end)

		afterEach(function()
			if mock_rig then
				mock_rig:Destroy()
			end
		end)

		it("should initialize correctly and build the bone hierarchy", function()
			local rig = rig_module.new(mock_rig)

			expect(rig).to.be.ok()
			expect(rig.model).to.equal(mock_rig)
			expect(rig.root.part.Name).to.equal("HumanoidRootPart")
			expect(#rig.root.children).to.equal(1)
			
			local torso_rigpart = rig.root.children[1]
			expect(torso_rigpart.part.Name).to.equal("Torso")
			expect(#torso_rigpart.children).to.equal(1)

			local head_rigpart = torso_rigpart.children[1]
			expect(head_rigpart.part.Name).to.equal("Head")
		end)

		it("should perform an animation data round-trip", function()
			local rig = rig_module.new(mock_rig)

			local anim_data = {
				t = 1.0,
				kfs = {
					{
						t = 0.0,
						kf = {
							Head = {0, 1, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1}, -- CFrame components for position (0, 1, 0)
						}
					},
					{
						t = 1.0,
						kf = {
							Head = {0, 2, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1}, -- CFrame components for position (0, 2, 0)
						}
					}
				}
			}

			rig:LoadAnimation(anim_data)
			local kfs = rig:ToRobloxAnimation()

			expect(kfs).to.be.ok()
			local keyframes = kfs:GetKeyframes()
			expect(#keyframes).to.equal(2)
			
			-- Verify first keyframe
			local kf1 = keyframes[1]
			expect(kf1.Time).to.be.near(0.0)
			local head_pose1 = kf1.HumanoidRootPart.Torso.Head
			expect(head_pose1).to.be.ok()
			expect(head_pose1.CFrame).to.equal(CFrame.new(0, 1, 0))

			-- Verify second keyframe
			local kf2 = keyframes[2]
			expect(kf2.Time).to.be.near(1.0)
			local head_pose2 = kf2.HumanoidRootPart.Torso.Head
			expect(head_pose2).to.be.ok()
			expect(head_pose2.CFrame).to.equal(CFrame.new(0, 2, 0))
		end)
		it("should correctly handle a variety of easing styles in a round-trip", function()
			local rig = rig_module.new(mock_rig)

			-- Create a KeyframeSequence with various easing styles
			local kfs_in = Instance.new("KeyframeSequence")
			local styles = { "Linear", "Constant", "Cubic", "Bounce", "Elastic" }
			local directions = { "In", "Out", "InOut" }
			
			local i = 1
			for _, style in ipairs(styles) do
				for _, dir in ipairs(directions) do
					local kf = Instance.new("Keyframe")
					kf.Time = i
					local pose = Instance.new("Pose")
					pose.Name = "Head"
					pose.EasingStyle = Enum.PoseEasingStyle[style]
					pose.EasingDirection = Enum.PoseEasingDirection[dir]
					pose.Parent = kf
					kf.Parent = kfs_in
					i = i + 1
				end
			end
			
			-- Serialize and deserialize
			local serializer = require(script.Parent.Parent.Components.AnimationSerializer).new()
			local serialized_data = serializer:serialize(kfs_in, rig)
			rig:LoadAnimation(serialized_data)
			
			-- Convert back to KeyframeSequence
			local kfs_out = rig:ToRobloxAnimation()
			local keyframes_out = kfs_out:GetKeyframes()

			-- Verify
			expect(#keyframes_out).to.equal(#styles * #directions) -- It should be exactly the number of keyframes we created

			local i = 1
			for _, style in ipairs(styles) do
				for _, dir in ipairs(directions) do
					-- Find the keyframe at the correct, NORMALIZED time
					local time_to_find = i - 1
					local kf_out = nil
					for _, kf in ipairs(keyframes_out) do
						if math.abs(kf.Time - time_to_find) < 0.001 then
							kf_out = kf
							break
						end
					end
					
					expect(kf_out).to.be.ok()
					local pose_out = kf_out:FindFirstChild("Head", true)
					expect(pose_out).to.be.ok()
					expect(pose_out.EasingStyle.Name).to.equal(style)
					expect(pose_out.EasingDirection.Name).to.equal(dir)
					i = i + 1
				end
			end
		end)

		it("should correctly handle named keyframes", function()
			local rig = rig_module.new(mock_rig)
			rig.keyframeNames = {
				{ time = 0.5, name = "Halfway" },
				{ time = 1.0, name = "End" },
			}

			local anim_data = {
				t = 1.0,
				kfs = {
					{ t = 0.0, kf = {} },
					{ t = 0.5, kf = {} },
					{ t = 1.0, kf = {} },
				}
			}
			
			rig:LoadAnimation(anim_data)
			local kfs = rig:ToRobloxAnimation()

			local keyframes = kfs:GetKeyframes()
			expect(#keyframes).to.equal(3)
			expect(keyframes[1].Name).to.equal("Keyframe") -- Default name
			expect(keyframes[2].Name).to.equal("Halfway")
			expect(keyframes[3].Name).to.equal("End")
		end)

		it("should handle animation data with no keyframes", function()
			local rig = rig_module.new(mock_rig)
			local anim_data = { t = 0, kfs = {} }

			rig:LoadAnimation(anim_data)
			local kfs = rig:ToRobloxAnimation()

			expect(kfs).to.be.ok()
			-- It should create one keyframe at t=0
			expect(#kfs:GetKeyframes()).to.equal(1)
			expect(kfs:GetKeyframes()[1].Time).to.be.near(0)
		end)

		it("should throw an error for invalid animation data", function()
			local rig = rig_module.new(mock_rig)
			
			expect(function() rig:LoadAnimation(nil) end).to.throw()
			expect(function() rig:LoadAnimation({ t = 1.0 }) end).to.throw() -- missing kfs
			expect(function() rig:LoadAnimation({ kfs = {} }) end).to.throw() -- missing t
		end)

		it("should handle a rig with no PrimaryPart", function()
			mock_rig.PrimaryPart = nil
			local rig = rig_module.new(mock_rig)
			expect(rig.root).to.equal(nil)
		end)

		it("should handle a hybrid rig with both Motor6D and Bone instances", function()
			-- Add a bone to the torso of our existing mock rig
			local shoulder_bone = Instance.new("Bone")
			shoulder_bone.Name = "ShoulderBone"
			shoulder_bone.Parent = mock_rig:FindFirstChild("Torso")

			local arm_bone = Instance.new("Bone")
			arm_bone.Name = "ArmBone"
			arm_bone.Parent = shoulder_bone

			local rig = rig_module.new(mock_rig)
			
			-- The rig should detect the presence of bones
			expect(rig.isDeformRig).to.equal(true)

			-- It should still find the Motor6D-connected parts
			expect(rig.bones["Head"]).to.be.ok()
			
			-- And it should also find the Bone-instance parts
			expect(rig.bones["ShoulderBone"]).to.be.ok()
			expect(rig.bones["ArmBone"]).to.be.ok()
		end)

		it("should handle a pure deform rig with only Bones", function()
			local deform_rig = Instance.new("Model")
			deform_rig.Name = "DeformRig"

			local root_part = Instance.new("Part")
			root_part.Name = "RootPart"
			root_part.Parent = deform_rig
			deform_rig.PrimaryPart = root_part

			local spine_bone = Instance.new("Bone")
			spine_bone.Name = "Spine"
			spine_bone.Parent = root_part

			local head_bone = Instance.new("Bone")
			head_bone.Name = "Head"
			head_bone.Parent = spine_bone

			local rig = rig_module.new(deform_rig)

			expect(rig).to.be.ok()
			expect(rig.isDeformRig).to.equal(true)
			expect(rig.bones["Spine"]).to.be.ok()
			expect(rig.bones["Head"]).to.be.ok()

			local spine_rigpart = rig.root.children[1]
			expect(spine_rigpart.part.Name).to.equal("Spine")
			expect(#spine_rigpart.children).to.equal(1)

			local head_rigpart = spine_rigpart.children[1]
			expect(head_rigpart.part.Name).to.equal("Head")

			deform_rig:Destroy()
		end)

		it("should correctly encode the rig structure", function()
			local rig = rig_module.new(mock_rig)
			local encoded_rig = rig:EncodeRig()

			expect(encoded_rig).to.be.ok()
			expect(encoded_rig.inst).to.equal(mock_rig.PrimaryPart)
			expect(encoded_rig.jname).to.equal("HumanoidRootPart")
			expect(#encoded_rig.children).to.equal(1)

			local torso_encoded = encoded_rig.children[1]
			expect(torso_encoded.jname).to.equal("Torso")
			expect(#torso_encoded.children).to.equal(1)
			
			local head_encoded = torso_encoded.children[1]
			expect(head_encoded.jname).to.equal("Head")
			expect(#head_encoded.children).to.equal(0)
		end)
		it("should correctly set pose weights based on the 'enabled' property", function()
			local rig = rig_module.new(mock_rig)

			-- Explicitly enable/disable specific parts
			local head_part = rig.bones["Head"]
			local torso_part = rig.bones["Torso"]
			
			expect(head_part).to.be.ok()
			expect(torso_part).to.be.ok()

			head_part.enabled = true
			torso_part.enabled = false
			rig.root.enabled = true -- Root is always enabled

			-- Create animation data that targets both parts
			local anim_data = {
				t = 1.0,
				kfs = {
					{
						t = 0.0,
						kf = {
							Head = { 0, 1, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1 },
							Torso = { 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1 },
						},
					},
				},
			}
			rig:LoadAnimation(anim_data)

			-- Generate the KeyframeSequence
			local kfs = rig:ToRobloxAnimation()
			expect(kfs).to.be.ok()

			-- Verify that the weights are set correctly
			local keyframe = kfs:GetKeyframes()[1]
			expect(keyframe).to.be.ok()
			
			local root_pose = keyframe:FindFirstChild("HumanoidRootPart", true)
			local torso_pose = keyframe:FindFirstChild("Torso", true)
			local head_pose = keyframe:FindFirstChild("Head", true)

			expect(root_pose).to.be.ok()
			expect(torso_pose).to.be.ok()
			expect(head_pose).to.be.ok()

			expect(root_pose.Weight).to.equal(1)
			expect(head_pose.Weight).to.equal(1)
			expect(torso_pose.Weight).to.equal(0)
		end)
	end)

	describe("Sync Bones Positioning Debug", function()
		local testRig
		local rigManager
		local originalGetBoneRest
		local originalSettings

		beforeEach(function()
			-- Create a test rig
			testRig = Instance.new("Model")
			testRig.Name = "TestRig"

			local hrp = Instance.new("Part")
			hrp.Name = "HumanoidRootPart"
			hrp.Size = Vector3.new(2, 2, 1)
			hrp.CFrame = CFrame.new(0, 5, 0)
			hrp.Anchored = true
			hrp.Parent = testRig
			testRig.PrimaryPart = hrp

			local humanoid = Instance.new("Humanoid")
			humanoid.Parent = testRig

			-- Add some test parts with known positions
			local rightArm = Instance.new("Part")
			rightArm.Name = "RightArm"
			rightArm.Size = Vector3.new(1, 2, 1)
			rightArm.CFrame = hrp.CFrame * CFrame.new(1.5, 0, 0)
			rightArm.Parent = testRig

			local rightArmMotor = Instance.new("Motor6D")
			rightArmMotor.Name = "RightArm"
			rightArmMotor.Part0 = hrp
			rightArmMotor.Part1 = rightArm
			rightArmMotor.C0 = CFrame.new(1.5, 0, 0)
			rightArmMotor.C1 = CFrame.new()
			rightArmMotor.Parent = hrp

			-- Mock playback service
			local mockPlaybackService = {
				stopAnimationAndDisconnect = function(self, options)
					-- Do nothing
				end,
				playCurrentAnimation = function(self, animator)
					-- Do nothing
				end
			}

			-- Set up the rig manager
			rigManager = RigManager.new(mockPlaybackService, nil)

			-- Mock settings function
			local originalSettings = settings
			settings = function()
				return {
					Rendering = {
						ExportMergeByMaterial = false
					}
				}
			end

			-- Store original function for restoration
			originalGetBoneRest = BlenderConnection.GetBoneRest

			-- Mock the State for testing
			local originalActiveRigModel = State.activeRigModel
			local originalActiveRig = State.activeRig
			local originalActiveRigExists = State.activeRigExists:get()
			State.activeRigModel = testRig
			State.activeRigExists:set(true)

			-- Clean up after test
			testRig.Destroying:Connect(function()
				State.activeRigModel = originalActiveRigModel
				State.activeRigExists:set(originalActiveRigExists)
			end)
		end)

		afterEach(function()
			if testRig then
				testRig:Destroy()
			end
			-- Restore original functions
			if originalGetBoneRest then
				BlenderConnection.GetBoneRest = originalGetBoneRest
			end
			if originalSettings then
				settings = originalSettings
			end
		end)

		it("should debug bone positioning when syncing bones", function()
			print("\n=== SYNC BONES POSITIONING DEBUG ===")

			-- Create mock bone data that would come from Blender
			-- Format: {x, y, z, r00, r01, r02, r10, r11, r12, r20, r21, r22}
			local mockBonePoses = {
				["RightArm"] = {
					relative = {1.5, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1}, -- CFrame.new(1.5, 0, 0) components
					parent = "HumanoidRootPart",
					is_synthetic_helper = false
				},
				["RightHand"] = {
					relative = {0, -1, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1}, -- CFrame.new(0, -1, 0) components
					parent = "RightArm",
					is_synthetic_helper = true
				},
				["Sword_R"] = {
					relative = {0, -0.5, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1}, -- CFrame.new(0, -0.5, 0) components
					parent = "RightHand",
					is_synthetic_helper = true
				}
			}

			local mockBoneData = {
				armature = "TestArmature",
				bone_poses = mockBonePoses
			}

			-- Mock the BlenderConnection to return our test data
			BlenderConnection.GetBoneRest = function(self, port, armatureName)
				return mockBoneData
			end

			print("Original rig parts:")
			for _, part in ipairs(testRig:GetChildren()) do
				if part:IsA("BasePart") then
					print(string.format("  %s: Position = %s", part.Name, tostring(part.Position)))
				end
			end

			-- Test the sync bones function
			local rig = rig_module.new(testRig)
			State.activeRig = rig

			print("\nRig hierarchy before sync:")
			if rig.root then
				local function printHierarchy(node, depth)
					local indent = string.rep("  ", depth)
					print(string.format("%s%s", indent, node.part.Name))
					for _, child in ipairs(node.children) do
						printHierarchy(child, depth + 1)
					end
				end
				printHierarchy(rig.root, 0)
			end

			-- Create a mock connection service that returns our test data
			local mockConnectionService = {
				GetBoneRest = function(self, port, armatureName)
					return mockBoneData
				end,
				ListArmatures = function(self, port)
					return {{
						name = "TestArmature",
						bones = {"HumanoidRootPart", "RightArm", "RightHand", "Sword_R"},
						bone_hierarchy = {
							RightArm = "HumanoidRootPart",
							RightHand = "RightArm",
							Sword_R = "RightHand"
						}
					}}
				end
			}

			-- Mock server connection
			State.isServerConnected:set(true)
			State.selectedArmature:set({name = "TestArmature"})

			print("\nSyncing bones...")
			local success = rigManager:syncBones({blenderConnectionService = mockConnectionService})

			print(string.format("Sync result: %s", success and "SUCCESS" or "FAILED"))

			print("\nRig parts after sync:")
			for _, part in ipairs(testRig:GetChildren()) do
				if part:IsA("BasePart") then
					print(string.format("  %s: Position = %s", part.Name, tostring(part.Position)))
				end
			end

			print("\nRig hierarchy after sync:")
			if rig.root then
				local function printHierarchy(node, depth)
					local indent = string.rep("  ", depth)
					print(string.format("%s%s", indent, node.part.Name))
					for _, child in ipairs(node.children) do
						printHierarchy(child, depth + 1)
					end
				end
				printHierarchy(rig.root, 0)
			end

			print("\nMotor6D joints after sync:")
			for _, descendant in ipairs(testRig:GetDescendants()) do
				if descendant:IsA("Motor6D") then
					local part0 = descendant.Part0
					local part1 = descendant.Part1
					print(string.format("  %s: %s -> %s", descendant.Name, part0 and part0.Name or "nil", part1 and part1.Name or "nil"))
					print(string.format("    C0 = %s", tostring(descendant.C0)))
					print(string.format("    C1 = %s", tostring(descendant.C1)))
					if part0 and part1 then
						local expectedPos = part0.CFrame * descendant.C0 * descendant.C1:Inverse()
						local actualPos = part1.CFrame
						local diff = (expectedPos.Position - actualPos.Position).Magnitude
						print(string.format("    Expected pos: %s", tostring(expectedPos.Position)))
						print(string.format("    Actual pos: %s", tostring(actualPos.Position)))
						print(string.format("    Position difference: %.6f", diff))
					end
				end
			end

			-- Restore original function
			BlenderConnection.GetBoneRest = originalGetBoneRest

			-- The test passes if we get here without errors
			expect(success).to.be.ok()
		end)
	end)
end 