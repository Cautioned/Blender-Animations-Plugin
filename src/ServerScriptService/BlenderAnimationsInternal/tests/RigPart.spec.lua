return function()
	local RigPart = require(script.Parent.Parent.Components.RigPart)

	describe("RigPart", function()
		-- Helper: minimal rig stub that satisfies the constructor
		local function makeRig(jointCache)
			return {
				bones = {},
				bonesByInstance = {},
				_jointCache = jointCache or {},
				_duplicateBoneWarnings = {},
				model = nil,
			}
		end

		-- Helper: build a simple Motor6D chain and return { rig, parts, motors }
		local function buildChain(names: { string })
			local parts = {}
			local motors = {}
			local jointCache = {}

			for i, name in names do
				local part = Instance.new("Part")
				part.Name = name
				parts[i] = part
				jointCache[part] = {}
			end

			for i = 2, #parts do
				local motor = Instance.new("Motor6D")
				motor.Name = parts[i].Name .. "Joint"
				motor.Part0 = parts[i - 1]
				motor.Part1 = parts[i]
				motor.Parent = parts[i - 1]
				motors[i - 1] = motor
				table.insert(jointCache[parts[i - 1]], motor)
				table.insert(jointCache[parts[i]], motor)
			end

			local rig = makeRig(jointCache)
			return rig, parts, motors
		end

		describe("construction and hierarchy", function()
			it("should build a linear chain of children", function()
				local rig, parts = buildChain({ "Root", "Torso", "Head" })

				local root = RigPart.new(rig, parts[1], nil, false)

				expect(root).to.be.ok()
				expect(#root.children).to.equal(1)
				expect(root.children[1].part.Name).to.equal("Torso")
				expect(#root.children[1].children).to.equal(1)
				expect(root.children[1].children[1].part.Name).to.equal("Head")

				for _, p in parts do p:Destroy() end
			end)

			it("should build a branching hierarchy", function()
				local root = Instance.new("Part")
				root.Name = "Root"
				local left = Instance.new("Part")
				left.Name = "LeftArm"
				local right = Instance.new("Part")
				right.Name = "RightArm"

				local m1 = Instance.new("Motor6D")
				m1.Part0 = root
				m1.Part1 = left
				m1.Parent = root

				local m2 = Instance.new("Motor6D")
				m2.Part0 = root
				m2.Part1 = right
				m2.Parent = root

				local jointCache = {
					[root] = { m1, m2 },
					[left] = { m1 },
					[right] = { m2 },
				}
				local rig = makeRig(jointCache)
				local rp = RigPart.new(rig, root, nil, false)

				expect(#rp.children).to.equal(2)
				local childNames = {}
				for _, c in rp.children do
					childNames[c.part.Name] = true
				end
				expect(childNames["LeftArm"]).to.equal(true)
				expect(childNames["RightArm"]).to.equal(true)

				root:Destroy()
				left:Destroy()
				right:Destroy()
			end)

			it("should silently prune already-visited parts (cycle guard)", function()
				-- visitedAll catches revisits before pathSet cycle detection,
				-- so cycles are handled by returning nil (pruning) not throwing
				local a = Instance.new("Part")
				a.Name = "A"
				local b = Instance.new("Part")
				b.Name = "B"

				local m1 = Instance.new("Motor6D")
				m1.Part0 = a
				m1.Part1 = b
				m1.Parent = a

				local m2 = Instance.new("Motor6D")
				m2.Part0 = b
				m2.Part1 = a
				m2.Parent = b

				local jointCache = {
					[a] = { m1, m2 },
					[b] = { m1, m2 },
				}
				local rig = makeRig(jointCache)

				-- should not throw; visitedAll prunes the back-edge
				local rp
				expect(function()
					rp = RigPart.new(rig, a, nil, false)
				end).to.never.throw()

				-- A->B built fine, B->A was pruned
				expect(rp).to.be.ok()
				expect(#rp.children).to.equal(1)
				expect(rp.children[1].part.Name).to.equal("B")

				a:Destroy()
				b:Destroy()
			end)

			it("should throw on depth overflow", function()
				-- force a very low max depth to trigger the depth guard
				local parts = {}
				local jointCache = {}
				for i = 1, 5 do
					local p = Instance.new("Part")
					p.Name = "P" .. i
					parts[i] = p
					jointCache[p] = {}
				end
				for i = 1, 4 do
					local m = Instance.new("Motor6D")
					m.Part0 = parts[i]
					m.Part1 = parts[i + 1]
					m.Parent = parts[i]
					table.insert(jointCache[parts[i]], m)
					table.insert(jointCache[parts[i + 1]], m)
				end

				local rig = makeRig(jointCache)
				local state = {
					depth = 0,
					maxDepth = 2, -- only allow 2 levels
					path = {},
					pathSet = {},
					visitedAll = {},
				}

				expect(function()
					RigPart.new(rig, parts[1], nil, false, nil, state)
				end).to.throw()

				for _, p in parts do p:Destroy() end
			end)

			it("should track jointParentIsPart0 correctly", function()
				local parent = Instance.new("Part")
				parent.Name = "Parent"
				local child = Instance.new("Part")
				child.Name = "Child"

				-- standard: Part0=parent, Part1=child
				local motor = Instance.new("Motor6D")
				motor.Part0 = parent
				motor.Part1 = child
				motor.Parent = parent

				local jointCache = {
					[parent] = { motor },
					[child] = { motor },
				}
				local rig = makeRig(jointCache)
				local rp = RigPart.new(rig, parent, nil, false)

				expect(rp.children[1].jointParentIsPart0).to.equal(true)

				-- reversed: Part0=child, Part1=parent
				local parent2 = Instance.new("Part")
				parent2.Name = "Parent2"
				local child2 = Instance.new("Part")
				child2.Name = "Child2"
				local motor2 = Instance.new("Motor6D")
				motor2.Part0 = child2
				motor2.Part1 = parent2
				motor2.Parent = parent2

				local jc2 = {
					[parent2] = { motor2 },
					[child2] = { motor2 },
				}
				local rig2 = makeRig(jc2)
				local rp2 = RigPart.new(rig2, parent2, nil, false)

				expect(rp2.children[1].jointParentIsPart0).to.equal(false)

				parent:Destroy()
				child:Destroy()
				parent2:Destroy()
				child2:Destroy()
			end)

			it("should register parts in rig.bones by name", function()
				local rig, parts = buildChain({ "Root", "Torso" })
				RigPart.new(rig, parts[1], nil, false)

				expect(rig.bones["Root"]).to.be.ok()
				expect(rig.bones["Torso"]).to.be.ok()

				for _, p in parts do p:Destroy() end
			end)
		end)

		describe("AddPose", function()
			it("should store a pose at the given time", function()
				local rig, parts = buildChain({ "Root" })
				local rp = RigPart.new(rig, parts[1], nil, false)

				rp:AddPose(0.5, CFrame.new(1, 2, 3), false, "Linear", "In")

				expect(rp.poses[0.5]).to.be.ok()
				expect(rp.poses[0.5].transform.Position.X).to.be.near(1, 0.001)
				expect(rp.poses[0.5].easingStyle).to.equal("Linear")

				for _, p in parts do p:Destroy() end
			end)
		end)

		describe("PoseToRobloxAnimation", function()
			-- helper to build a single-part RigPart with poses
			local function singlePartWithPoses(posesData)
				local part = Instance.new("Part")
				part.Name = "TestPart"
				local rig = makeRig({ [part] = {} })
				local rp = RigPart.new(rig, part, nil, false)
				for t, data in posesData do
					rp:AddPose(t, data.cf, false, data.easing or "Linear", data.dir or "In")
				end
				return rp, part
			end

			it("should create a Pose instance at an exact keyframe time", function()
				local rp, part = singlePartWithPoses({
					[0] = { cf = CFrame.new(1, 0, 0) },
					[1] = { cf = CFrame.new(2, 0, 0) },
				})

				local pose = rp:PoseToRobloxAnimation(0)

				expect(pose).to.be.ok()
				expect(pose.Name).to.equal("TestPart")
				expect(pose.CFrame.Position.X).to.be.near(1, 0.001)
				expect(pose.Weight).to.equal(1)

				part:Destroy()
			end)

			it("should interpolate linearly between keyframes", function()
				local rp, part = singlePartWithPoses({
					[0] = { cf = CFrame.new(0, 0, 0) },
					[1] = { cf = CFrame.new(10, 0, 0) },
				})

				local pose = rp:PoseToRobloxAnimation(0.5)

				expect(pose).to.be.ok()
				expect(pose.CFrame.Position.X).to.be.near(5, 0.001)

				part:Destroy()
			end)

			it("should hold previous value for Constant easing", function()
				local rp, part = singlePartWithPoses({
					[0] = { cf = CFrame.new(0, 0, 0), easing = "Constant" },
					[1] = { cf = CFrame.new(10, 0, 0) },
				})

				local pose = rp:PoseToRobloxAnimation(0.5)

				expect(pose).to.be.ok()
				-- constant hold = previous value, which is 0
				expect(pose.CFrame.Position.X).to.be.near(0, 0.001)

				part:Destroy()
			end)

			it("should return nil when no poses exist and no children", function()
				local part = Instance.new("Part")
				part.Name = "Empty"
				local rig = makeRig({ [part] = {} })
				local rp = RigPart.new(rig, part, nil, false)

				local pose = rp:PoseToRobloxAnimation(0.5)
				expect(pose).to.never.be.ok()

				part:Destroy()
			end)

			it("should set weight to 0 when disabled", function()
				local part = Instance.new("Part")
				part.Name = "Disabled"
				local rig = makeRig({ [part] = {} })
				local rp = RigPart.new(rig, part, nil, false)
				rp.enabled = false
				rp:AddPose(0, CFrame.new(), false, "Linear", "In")

				local pose = rp:PoseToRobloxAnimation(0)
				expect(pose).to.be.ok()
				expect(pose.Weight).to.equal(0)

				part:Destroy()
			end)

			it("should fall back to nearest pose when time is before first keyframe", function()
				local rp, part = singlePartWithPoses({
					[1] = { cf = CFrame.new(5, 0, 0) },
				})

				local pose = rp:PoseToRobloxAnimation(0)
				expect(pose).to.be.ok()
				expect(pose.CFrame.Position.X).to.be.near(5, 0.001)

				part:Destroy()
			end)

			it("should fall back to nearest pose when time is after last keyframe", function()
				local rp, part = singlePartWithPoses({
					[0] = { cf = CFrame.new(3, 0, 0) },
				})

				local pose = rp:PoseToRobloxAnimation(5)
				expect(pose).to.be.ok()
				expect(pose.CFrame.Position.X).to.be.near(3, 0.001)

				part:Destroy()
			end)

			it("should include children poses as sub-poses", function()
				local root = Instance.new("Part")
				root.Name = "Root"
				local child = Instance.new("Part")
				child.Name = "Child"
				local motor = Instance.new("Motor6D")
				motor.Part0 = root
				motor.Part1 = child
				motor.Parent = root

				local jc = { [root] = { motor }, [child] = { motor } }
				local rig = makeRig(jc)
				local rp = RigPart.new(rig, root, nil, false)

				rp:AddPose(0, CFrame.new(1, 0, 0), false, "Linear", "In")
				rp.children[1]:AddPose(0, CFrame.new(0, 2, 0), false, "Linear", "In")

				local pose = rp:PoseToRobloxAnimation(0)
				expect(pose).to.be.ok()

				local subPoses = pose:GetChildren()
				expect(#subPoses).to.equal(1)
				expect(subPoses[1].Name).to.equal("Child")
				expect(subPoses[1].CFrame.Position.Y).to.be.near(2, 0.001)

				root:Destroy()
				child:Destroy()
			end)

			it("should apply correct easing style enum", function()
				local rp, part = singlePartWithPoses({
					[0] = { cf = CFrame.new(), easing = "Cubic", dir = "Out" },
				})

				local pose = rp:PoseToRobloxAnimation(0)
				expect(pose.EasingStyle).to.equal(Enum.PoseEasingStyle.Cubic)
				expect(pose.EasingDirection).to.equal(Enum.PoseEasingDirection.Out)

				part:Destroy()
			end)

			it("should carry forward easing direction on synthetic interpolated poses", function()
				local rp, part = singlePartWithPoses({
					[0] = { cf = CFrame.new(0, 0, 0), easing = "Linear", dir = "InOut" },
					[2] = { cf = CFrame.new(10, 0, 0), easing = "Linear", dir = "In" },
				})

				-- At t=1, synthetic pose should carry prevPose's direction (InOut)
				local pose = rp:PoseToRobloxAnimation(1)
				expect(pose).to.be.ok()
				expect(pose.EasingDirection).to.equal(Enum.PoseEasingDirection.InOut)
				-- Value should be linearly interpolated
				expect(pose.CFrame.Position.X).to.be.near(5, 0.001)

				part:Destroy()
			end)

			it("should create identity pose for structural parent with children but no own poses", function()
				-- Parent has NO poses, but child does → parent should still emit
				-- a Pose (at identity) so the child sub-pose can be parented
				local root = Instance.new("Part")
				root.Name = "Root"
				local child = Instance.new("Part")
				child.Name = "Child"
				local motor = Instance.new("Motor6D")
				motor.Part0 = root
				motor.Part1 = child
				motor.Parent = root

				local jc = { [root] = { motor }, [child] = { motor } }
				local rig = makeRig(jc)
				local rp = RigPart.new(rig, root, nil, false)

				-- Only child has a pose, parent does not
				rp.children[1]:AddPose(0, CFrame.new(5, 0, 0), false, "Linear", "In")

				local pose = rp:PoseToRobloxAnimation(0)
				expect(pose).to.be.ok()
				expect(pose.Name).to.equal("Root")
				-- Parent should have identity CFrame (default)
				expect(pose.CFrame.Position.Magnitude).to.be.near(0, 0.001)
				-- Child sub-pose should be present
				local subPoses = pose:GetChildren()
				expect(#subPoses).to.equal(1)
				expect(subPoses[1].CFrame.Position.X).to.be.near(5, 0.001)

				root:Destroy()
				child:Destroy()
			end)

			it("should synthesize fill for parent with children when between keyframes", function()
				-- Parent has keyframes at t=0 and t=2, child has keyframe at t=0,1,2
				-- At t=1 parent should get an interpolated synthetic value
				local root = Instance.new("Part")
				root.Name = "Root"
				local child = Instance.new("Part")
				child.Name = "Child"
				local motor = Instance.new("Motor6D")
				motor.Part0 = root
				motor.Part1 = child
				motor.Parent = root

				local jc = { [root] = { motor }, [child] = { motor } }
				local rig = makeRig(jc)
				local rp = RigPart.new(rig, root, nil, false)

				rp:AddPose(0, CFrame.new(0, 0, 0), false, "Linear", "In")
				rp:AddPose(2, CFrame.new(10, 0, 0), false, "Linear", "In")
				rp.children[1]:AddPose(0, CFrame.new(0, 1, 0), false, "Linear", "In")
				rp.children[1]:AddPose(1, CFrame.new(0, 2, 0), false, "Linear", "In")
				rp.children[1]:AddPose(2, CFrame.new(0, 3, 0), false, "Linear", "In")

				-- At t=1, parent should be interpolated to (5,0,0)
				local pose = rp:PoseToRobloxAnimation(1)
				expect(pose).to.be.ok()
				expect(pose.CFrame.Position.X).to.be.near(5, 0.001)

				-- Child should have its exact pose at t=1
				local subPoses = pose:GetChildren()
				expect(#subPoses).to.equal(1)
				expect(subPoses[1].CFrame.Position.Y).to.be.near(2, 0.001)

				root:Destroy()
				child:Destroy()
			end)

			it("should hold previous pose for Constant easing with children", function()
				-- Same setup but Constant easing: parent should HOLD, not interpolate
				local root = Instance.new("Part")
				root.Name = "Root"
				local child = Instance.new("Part")
				child.Name = "Child"
				local motor = Instance.new("Motor6D")
				motor.Part0 = root
				motor.Part1 = child
				motor.Parent = root

				local jc = { [root] = { motor }, [child] = { motor } }
				local rig = makeRig(jc)
				local rp = RigPart.new(rig, root, nil, false)

				rp:AddPose(0, CFrame.new(0, 0, 0), false, "Constant", "In")
				rp:AddPose(2, CFrame.new(10, 0, 0), false, "Linear", "In")
				rp.children[1]:AddPose(1, CFrame.new(0, 5, 0), false, "Linear", "In")

				-- At t=1, Constant easing should hold the t=0 value (0,0,0)
				local pose = rp:PoseToRobloxAnimation(1)
				expect(pose).to.be.ok()
				expect(pose.CFrame.Position.X).to.be.near(0, 0.001)

				root:Destroy()
				child:Destroy()
			end)
		end)

		describe("Encode", function()
			it("should encode a root part with world CFrame", function()
				local part = Instance.new("Part")
				part.Name = "Root"
				part.CFrame = CFrame.new(10, 20, 30)
				part.Parent = workspace

				local rig = makeRig({ [part] = {} })
				local rp = RigPart.new(rig, part, nil, false)

				local encoded = rp:Encode()
				expect(encoded).to.be.ok()
				expect(encoded.jname).to.equal("Root")
				expect(encoded.transform).to.be.ok()
				-- transform[1..3] are X,Y,Z
				expect(encoded.transform[1]).to.be.near(10, 0.001)
				expect(encoded.transform[2]).to.be.near(20, 0.001)
				expect(encoded.transform[3]).to.be.near(30, 0.001)

				part:Destroy()
			end)

			it("should skip parts when exportEnabled is false", function()
				local part = Instance.new("Part")
				part.Name = "Hidden"
				local rig = makeRig({ [part] = {} })
				local rp = RigPart.new(rig, part, nil, false)
				rp.exportEnabled = false

				local encoded = rp:Encode()
				expect(encoded).to.never.be.ok()

				part:Destroy()
			end)

			it("should skip Weld-connected children when exportWelds is false", function()
				local root = Instance.new("Part")
				root.Name = "Root"
				root.Parent = workspace

				local child = Instance.new("Part")
				child.Name = "WeldChild"
				child.Parent = workspace

				local weld = Instance.new("Weld")
				weld.Part0 = root
				weld.Part1 = child
				weld.Parent = root

				local jc = { [root] = { weld }, [child] = { weld } }
				local rig = makeRig(jc)
				local rp = RigPart.new(rig, root, nil, false)

				local encoded = rp:Encode({}, { exportWelds = false })
				expect(#encoded.children).to.equal(0)

				root:Destroy()
				child:Destroy()
			end)

			it("should include Weld-connected children when exportWelds is true", function()
				local model = Instance.new("Model")
				model.Name = "TestModel"
				model.Parent = workspace

				local root = Instance.new("Part")
				root.Name = "Root"
				root.Parent = model

				local child = Instance.new("Part")
				child.Name = "WeldChild"
				child.Parent = model

				local weld = Instance.new("Weld")
				weld.Part0 = root
				weld.Part1 = child
				weld.Parent = root

				local jc = { [root] = { weld }, [child] = { weld } }
				local rig = makeRig(jc)
				rig.model = model -- FindAuxPartsLegacy needs rig.model
				local rp = RigPart.new(rig, root, nil, false)

				local encoded = rp:Encode({}, { exportWelds = true })
				expect(#encoded.children).to.equal(1)
				expect(encoded.children[1].jname).to.equal("WeldChild")

				model:Destroy()
			end)

			it("should normalize C0/C1 based on joint direction", function()
				local parent = Instance.new("Part")
				parent.Name = "Parent"
				parent.Parent = workspace

				local child = Instance.new("Part")
				child.Name = "Child"
				child.Parent = workspace

				-- reversed: Part0=child, Part1=parent
				local motor = Instance.new("Motor6D")
				motor.Part0 = child
				motor.Part1 = parent
				motor.C0 = CFrame.new(1, 0, 0) -- child-relative
				motor.C1 = CFrame.new(0, 2, 0) -- parent-relative
				motor.Parent = parent

				local jc = { [parent] = { motor }, [child] = { motor } }
				local rig = makeRig(jc)
				local rp = RigPart.new(rig, parent, nil, false)

				local encoded = rp:Encode()
				local childEncoded = encoded.children[1]

				-- jointtransform0 should be parent-relative (C1 since reversed)
				expect(childEncoded.jointtransform0[2]).to.be.near(2, 0.001) -- Y from C1
				-- jointtransform1 should be child-relative (C0 since reversed)
				expect(childEncoded.jointtransform1[1]).to.be.near(1, 0.001) -- X from C0

				parent:Destroy()
				child:Destroy()
			end)

			it("should encode deform bones with WorldCFrame", function()
				local mesh = Instance.new("MeshPart")
				mesh.Name = "Body"
				mesh.Parent = workspace

				local bone = Instance.new("Bone")
				bone.Name = "Spine"
				bone.Parent = mesh

				local rig = makeRig({ [mesh] = {}, [bone] = {} })
				rig.model = mesh
				local rootRp = RigPart.new(rig, mesh, nil, true)

				-- manually build the bone child (normally done by Rig:buildBoneHierarchy)
				local boneRp = RigPart.new(rig, bone, rootRp, true)
				table.insert(rootRp.children, boneRp)

				local encoded = rootRp:Encode()
				-- root should encode fine
				expect(encoded).to.be.ok()
				expect(encoded.jname).to.equal("Body")

				mesh:Destroy()
			end)
		end)
	end)
end
