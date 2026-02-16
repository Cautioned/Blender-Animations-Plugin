return function()
	local Utils = require(script.Parent.Parent.Utils)

	describe("Utils", function()
		describe("getAnimDuration", function()
			it("should return 0 for nil input", function()
				expect(Utils.getAnimDuration(nil)).to.equal(0)
			end)

			it("should return 0 for empty table", function()
				expect(Utils.getAnimDuration({})).to.equal(0)
			end)

			it("should return the max Time from a list of keyframes", function()
				local kfs = {
					{ Time = 0 },
					{ Time = 0.5 },
					{ Time = 1.2 },
					{ Time = 0.8 },
				}
				expect(Utils.getAnimDuration(kfs)).to.be.near(1.2, 0.001)
			end)

			it("should handle a single keyframe", function()
				expect(Utils.getAnimDuration({ { Time = 3.0 } })).to.be.near(3.0, 0.001)
			end)

			it("should handle keyframes at time 0", function()
				local kfs = { { Time = 0 }, { Time = 0 } }
				expect(Utils.getAnimDuration(kfs)).to.equal(0)
			end)
		end)

		describe("getRealKeyframeDuration", function()
			it("should return 0 for empty list", function()
				expect(Utils.getRealKeyframeDuration({})).to.equal(0)
			end)

			it("should only consider Keyframe instances", function()
				local kf1 = Instance.new("Keyframe")
				kf1.Time = 0.5

				local kf2 = Instance.new("Keyframe")
				kf2.Time = 2.0

				-- a non-Keyframe instance mixed in
				local folder = Instance.new("Folder")

				local result = Utils.getRealKeyframeDuration({ kf1, folder, kf2 })
				expect(result).to.be.near(2.0, 0.001)

				kf1:Destroy()
				kf2:Destroy()
				folder:Destroy()
			end)

			it("should return 0 when no Keyframe instances present", function()
				local folder = Instance.new("Folder")
				expect(Utils.getRealKeyframeDuration({ folder })).to.equal(0)
				folder:Destroy()
			end)

			it("should handle unsorted keyframes", function()
				local kf1 = Instance.new("Keyframe")
				kf1.Time = 3.0
				local kf2 = Instance.new("Keyframe")
				kf2.Time = 1.0
				local kf3 = Instance.new("Keyframe")
				kf3.Time = 5.0

				expect(Utils.getRealKeyframeDuration({ kf1, kf2, kf3 })).to.be.near(5.0, 0.001)

				kf1:Destroy()
				kf2:Destroy()
				kf3:Destroy()
			end)
		end)

		describe("scaleAnimation", function()
			it("should scale pose positions by the factor", function()
				local kfs = Instance.new("KeyframeSequence")
				local kf = Instance.new("Keyframe")
				kf.Time = 0
				kf.Parent = kfs

				local pose = Instance.new("Pose")
				pose.Name = "Torso"
				pose.CFrame = CFrame.new(2, 4, 6)
				pose.Parent = kf

				local scaled = Utils.scaleAnimation(kfs, 3)
				local scaledPose = scaled:GetDescendants()[2] -- [1] is Keyframe, [2] is Pose

				-- position should be 3x
				expect(scaledPose.CFrame.Position.X).to.be.near(6, 0.001)
				expect(scaledPose.CFrame.Position.Y).to.be.near(12, 0.001)
				expect(scaledPose.CFrame.Position.Z).to.be.near(18, 0.001)

				kfs:Destroy()
				scaled:Destroy()
			end)

			it("should preserve rotation when scaling", function()
				local kfs = Instance.new("KeyframeSequence")
				local kf = Instance.new("Keyframe")
				kf.Time = 0
				kf.Parent = kfs

				local angle = math.rad(45)
				local pose = Instance.new("Pose")
				pose.Name = "Part"
				pose.CFrame = CFrame.new(1, 1, 1) * CFrame.Angles(angle, 0, 0)
				pose.Parent = kf

				local scaled = Utils.scaleAnimation(kfs, 2)
				local scaledPose = scaled:GetDescendants()[2]

				-- position scaled
				expect(scaledPose.CFrame.Position.X).to.be.near(2, 0.001)

				-- rotation preserved — extract rx from the rotation matrix
				local _, _, _, _, _, _, _, r21, r22 = scaledPose.CFrame:GetComponents()
				local recoveredAngle = math.atan2(-r21, r22)
				-- atan2 approach depends on which axis; just check the lookVector is similar
				local origLook = (CFrame.Angles(angle, 0, 0)).LookVector
				local scaledLook = (scaledPose.CFrame - scaledPose.CFrame.Position).LookVector
				expect(origLook.X - scaledLook.X).to.be.near(0, 0.01)
				expect(origLook.Y - scaledLook.Y).to.be.near(0, 0.01)
				expect(origLook.Z - scaledLook.Z).to.be.near(0, 0.01)

				kfs:Destroy()
				scaled:Destroy()
			end)

			it("should not mutate the original KeyframeSequence", function()
				local kfs = Instance.new("KeyframeSequence")
				local kf = Instance.new("Keyframe")
				kf.Time = 0
				kf.Parent = kfs

				local pose = Instance.new("Pose")
				pose.Name = "Part"
				pose.CFrame = CFrame.new(1, 2, 3)
				pose.Parent = kf

				Utils.scaleAnimation(kfs, 10)

				-- original untouched
				expect(pose.CFrame.Position.X).to.be.near(1, 0.001)
				expect(pose.CFrame.Position.Y).to.be.near(2, 0.001)

				kfs:Destroy()
			end)

			it("should handle scale factor of 1 (identity)", function()
				local kfs = Instance.new("KeyframeSequence")
				local kf = Instance.new("Keyframe")
				kf.Time = 0
				kf.Parent = kfs

				local pose = Instance.new("Pose")
				pose.Name = "Part"
				pose.CFrame = CFrame.new(3, 5, 7)
				pose.Parent = kf

				local scaled = Utils.scaleAnimation(kfs, 1)
				local p = scaled:GetDescendants()[2]

				expect(p.CFrame.Position.X).to.be.near(3, 0.001)
				expect(p.CFrame.Position.Y).to.be.near(5, 0.001)
				expect(p.CFrame.Position.Z).to.be.near(7, 0.001)

				kfs:Destroy()
				scaled:Destroy()
			end)

			it("should throw for non-positive scale factor", function()
				local kfs = Instance.new("KeyframeSequence")

				expect(function()
					Utils.scaleAnimation(kfs, 0)
				end).to.throw()

				expect(function()
					Utils.scaleAnimation(kfs, -1)
				end).to.throw()

				kfs:Destroy()
			end)

			it("should handle fractional scale factors", function()
				local kfs = Instance.new("KeyframeSequence")
				local kf = Instance.new("Keyframe")
				kf.Time = 0
				kf.Parent = kfs

				local pose = Instance.new("Pose")
				pose.Name = "Part"
				pose.CFrame = CFrame.new(10, 20, 30)
				pose.Parent = kf

				local scaled = Utils.scaleAnimation(kfs, 0.5)
				local p = scaled:GetDescendants()[2]

				expect(p.CFrame.Position.X).to.be.near(5, 0.001)
				expect(p.CFrame.Position.Y).to.be.near(10, 0.001)
				expect(p.CFrame.Position.Z).to.be.near(15, 0.001)

				kfs:Destroy()
				scaled:Destroy()
			end)

			it("should scale all poses in a nested hierarchy", function()
				local kfs = Instance.new("KeyframeSequence")
				local kf = Instance.new("Keyframe")
				kf.Time = 0
				kf.Parent = kfs

				local parentPose = Instance.new("Pose")
				parentPose.Name = "Torso"
				parentPose.CFrame = CFrame.new(1, 2, 3)
				parentPose.Parent = kf

				local childPose = Instance.new("Pose")
				childPose.Name = "Head"
				childPose.CFrame = CFrame.new(0, 1, 0)
				childPose.Parent = parentPose

				local grandchildPose = Instance.new("Pose")
				grandchildPose.Name = "Jaw"
				grandchildPose.CFrame = CFrame.new(0, 0.5, 0)
				grandchildPose.Parent = childPose

				local scaled = Utils.scaleAnimation(kfs, 4)

				-- find all poses in the scaled clone
				local poseLookup = {}
				for _, desc in scaled:GetDescendants() do
					if desc:IsA("Pose") then
						poseLookup[desc.Name] = desc
					end
				end

				expect(poseLookup["Torso"].CFrame.Position.X).to.be.near(4, 0.001)
				expect(poseLookup["Torso"].CFrame.Position.Y).to.be.near(8, 0.001)
				expect(poseLookup["Head"].CFrame.Position.Y).to.be.near(4, 0.001)
				expect(poseLookup["Jaw"].CFrame.Position.Y).to.be.near(2, 0.001)

				kfs:Destroy()
				scaled:Destroy()
			end)

			it("should handle pose at origin without producing NaN", function()
				local kfs = Instance.new("KeyframeSequence")
				local kf = Instance.new("Keyframe")
				kf.Time = 0
				kf.Parent = kfs

				local pose = Instance.new("Pose")
				pose.Name = "Part"
				pose.CFrame = CFrame.new(0, 0, 0)
				pose.Parent = kf

				local scaled = Utils.scaleAnimation(kfs, 100)
				local p = scaled:GetDescendants()[2]

				expect(p.CFrame.Position.X).to.be.near(0, 0.001)
				expect(p.CFrame.Position.Y).to.be.near(0, 0.001)
				expect(p.CFrame.Position.Z).to.be.near(0, 0.001)
				-- ensure no NaN crept in
				expect(p.CFrame.Position.X == p.CFrame.Position.X).to.equal(true)

				kfs:Destroy()
				scaled:Destroy()
			end)
		end)
	end)
end
